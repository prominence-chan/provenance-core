"""Context compaction: when an agent's history is too big for where it is going, the old part of it becomes a written
brief and the newest turns stay as they were sent. The counterpart of prune.py: pruning removes what is dead weight
without changing what the model knows; compaction is for when that is not enough, and decides what to keep.

What the model is given instead of the history (docs/TECHNICAL.md, "Context compaction"):

    the system message(s) and the opening (everything before the agent's first turn: environment, task), verbatim
    one user message: the brief of turns 1..N
    turns N+1.. verbatim (every tool call there with its result, and the reverse)

The brief is extracted by rules, from that part of the history alone (no model call, a few milliseconds, the same
input always gives the same text): the user's own messages verbatim, the agent's last plan, every file it changed
with its current state (created / edited / deleted, how often, the last change), what failed and how, the commands
that ran with their outcome, the agent's own notes, the files it only read, and where it stood; with room to spare,
the latest text of the files and the latest search output. Optionally (`model: <tier>`) a cheap API model from the
router's pool writes a short narrative (decisions, dead ends, what is in progress) from that digest, never from the
raw history; the extracted sections stay, the model's text is bounded, timed out, and simply left out if anything
goes wrong. Measured on the owner's sessions, the model's text added little the rules had not kept and failed or
timed out on 1 call in 6 (docs/TECHNICAL.md), so it is off unless asked for.

Where the cut goes: the brief ends on every `step`-th assistant turn (the same grid as prune.py's stable boundary),
at the earliest such turn that leaves the rest within the budget, judged on the history as sent. As a conversation
grows the cut only moves forward, one grid step at a time, so between moves every request starts with the same
bytes, and a brief is computed once per cut (cached by a hash of the part it summarises) and reused."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import OrderedDict
from pathlib import Path

from . import prune
from .settings import scoped

VERSION = 1  # part of the cache key: a change to what the brief says makes old cached briefs unused

DEFAULTS = {
    "keep_turns": 4,  # at least the last 4 assistant turns (and what follows them) stay verbatim
    "step": 8,  # the cut sits on every 8th assistant turn (web: every 4th), so it moves seldom
    "brief_chars": 16000,  # the brief is at most this long ...
    "brief_share": 0.3,  # ... and at most this share of the budget
    "fill": 0.85,  # api: share of a model's context window a compacted request may use
    "min_context": 32000,  # api: models with a smaller context (tokens) are never given a compacted history
    "model": None,  # a tier (e.g. "spark", "ember") or API model id that writes the narrative; None: rules only
    "model_min_chars": 100000,  # ... only when the part summarised is at least this big
    "model_timeout_s": 30.0,
    "model_chars": 4000,  # hard bound on the model's text
    "model_input_chars": 40000,  # the digest the model reads
    "disk": True,  # model-written narratives are also kept under <PCORE_HOME>/compact (a restart reuses them)
    "use_room": False,  # the brief may grow into what the kept turns leave of the budget (its size then varies)
}
# a web chat's first message is matched by no cache: a finer grid keeps more turns verbatim, and the brief may use
# whatever room is left
SCOPE_DEFAULTS = {"web": {"step": 4, "use_room": True}}


def config(scope: str, value) -> dict | None:
    """The options for "api" (requests to API models whose context window is smaller than the history) or "web" (the
    web bridge's fresh prompts), or None when it is off there. Same forms as prune.config; PCORE_COMPACT overrides."""
    return scoped("COMPACT", scope, value, {**DEFAULTS, **SCOPE_DEFAULTS.get(scope, {})})


# ---- sizes ------------------------------------------------------------------------------------------------------------
def _text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    out = []
    for p in content if isinstance(content, list) else []:
        if isinstance(p, dict):
            out.append(p.get("text") or ("[image]" if "image" in str(p.get("type")) else ""))
    return "".join(out)


def json_size(m: dict) -> int:
    """A message's share of the router's token estimate (len(json.dumps(messages)) / 4)."""
    return len(json.dumps(m)) + 2


def rendered_size(m: dict) -> int:
    """About what chatgpt_web.render_prompt makes of a message (a little over, for JSON-escaped arguments)."""
    n = len(_text(m.get("content"))) + 24
    for tc in m.get("tool_calls") or []:
        fn = tc.get("function") or {}
        n += len(fn.get("arguments") or "") + len(fn.get("name") or "") + 40
    return n


# ---- where the cut goes -------------------------------------------------------------------------------------------------
def boundary(view: list[dict], budget: int, o: dict, measure=json_size) -> tuple[int, int, bool] | None:
    """(cut, opening, trim): the brief replaces view[opening:cut]; trim = even the latest allowed cut leaves too much
    and the kept turns' tool output has to be shortened. None: too few turns to summarise. The cut is the earliest
    turn on the grid (every `step`-th assistant turn, at least `keep_turns` from the end) where the opening, a
    full-size brief and the rest fit in `budget`; it depends on sizes only, never on what the brief says."""
    asst = [i for i, m in enumerate(view) if m.get("role") == "assistant"]
    keep, step = max(1, int(o["keep_turns"])), max(1, int(o["step"]))
    grid = [asst[j] for j in range(step, len(asst) - keep + 1, step)]
    if not grid:
        return None
    opening = asst[0]
    sizes = [measure(m) for m in view]
    after = [0] * (len(view) + 1)
    for i in range(len(view) - 1, -1, -1):
        after[i] = after[i + 1] + sizes[i]
    fixed = sum(sizes[:opening]) + brief_limit(budget, o)
    system = 0
    last = opening
    for g in grid:
        system += sum(sizes[i] for i in range(last, g) if view[i].get("role") in ("system", "developer"))
        last = g
        if fixed + system + after[g] <= budget:
            return g, opening, False
    return grid[-1], opening, True


def brief_limit(budget: int, o: dict) -> int:
    return max(1500, min(int(o["brief_chars"]), int(budget * float(o["brief_share"]))))


def _shape(m: dict) -> list:
    """What a message says, without call ids (a Responses client may regenerate them on resume)."""
    calls = [
        ((tc.get("function") or {}).get("name"), (tc.get("function") or {}).get("arguments"))
        for tc in m.get("tool_calls") or []
    ]
    return [m.get("role"), _text(m.get("content")), calls]


# ---- what the old part says ---------------------------------------------------------------------------------------------
_PATCH_SEC = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+?)\s*$")
_ERR = re.compile(
    r"error|Error|ERROR|FAIL|failed|Failed|Traceback|[Ee]xception|not found|No such file|denied|fatal|panic|refused"
)
_LOOK_WORDS = {
    "cat", "sed", "nl", "head", "tail", "bat", "ls", "rg", "grep", "find", "fd", "wc", "pwd", "echo", "printf",
    "which", "tree", "stat", "file", "du", "true", "awk", "sort", "uniq", "cut", "jq", "diff", "less", "date",
}  # fmt: skip
_GIT_LOOK = {"status", "diff", "log", "show", "branch", "rev-parse", "remote", "ls-files", "blame", "grep"}


def _clip(text: str, n: int) -> str:
    text = text.strip()
    if len(text) <= n:
        return text
    head = max(1, int(n * 0.7))
    tail = max(0, n - head - 7)
    return text[:head].rstrip() + " […] " + (text[-tail:].lstrip() if tail else "")


def _one_line(text: str, n: int) -> str:
    return _clip(" ".join(text.split()), n)


class Facts:
    """What the summarised part of a conversation shows, as data."""

    def __init__(self):
        self.users: list[tuple[int, str]] = []  # (the turn it came before, text)
        self.notes: list[tuple[int, str, bool]] = []  # (turn, text, an answer without tool calls?)
        self.plan: tuple[int, dict] | None = None
        self.files: dict[str, dict] = {}  # path -> {"changes": [...], "reads": n, "last_read": turn}
        self.commands: dict[str, dict] = {}  # command line -> {"turns", "code", "ok", "out", "failed_at"}
        self.failures: list[dict] = []  # {"turn", "what", "excerpt", "key"}
        self.looks: dict[str, tuple[int, str]] = {}  # a look-around command -> (turn, its latest output)
        self.turns = 0
        self.cwd: str | None = None

    def rel(self, p: str) -> str:
        c = self.cwd
        return p[len(c) + 1 :] if c and p.startswith(c.rstrip("/") + "/") else p


def extract(messages: list[dict], opening: int) -> Facts:
    """The facts in messages[opening:] (messages[:opening] are kept verbatim, so they are only read for the cwd)."""
    f = Facts()
    f.cwd = prune._session_cwd(messages)
    seen_users = {_text(m.get("content")).strip() for m in messages[:opening] if m.get("role") == "user"}
    results: dict[str, str] = {}
    for m in messages:
        if m.get("role") == "tool":
            results.setdefault(m.get("tool_call_id"), _text(m.get("content")))
    turn = 0
    for m in messages[opening:]:
        role = m.get("role")
        if role == "user":
            t = _text(m.get("content")).strip()
            if t and t not in seen_users:
                seen_users.add(t)
                f.users.append((turn + 1, t))
        elif role == "assistant":
            turn += 1
            t = _text(m.get("content")).strip()
            if t and not _filler(t):
                f.notes.append((turn, t, not m.get("tool_calls")))
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                _call(f, turn, fn.get("name") or "", fn.get("arguments"), results.get(tc.get("id")))
    f.turns = turn
    return f


def _filler(t: str) -> bool:
    return len(t) <= 200 and "\n" not in t and bool(prune._FILLER.match(t)) and not prune._SUBSTANCE.search(t)


_REFUSED = re.compile(r"\b(unknown|cannot|can't|unable|unsupported|refused|rejected|no such)\b", re.I)


def _ok(code: str | None, body: str) -> bool:
    if code is not None:
        return code == "0"
    return prune._succeeded(code, body) and not _REFUSED.search(body.lstrip()[:200])


def _file(f: Facts, p: str) -> dict:
    # text: (turn, how it was seen, the text) of the file's latest version the conversation shows, if any
    return f.files.setdefault(p, {"changes": [], "reads": 0, "last_read": 0, "text": None})


def _call(f: Facts, turn: int, name: str, arguments, result: str | None):
    c = prune.Call(name, arguments, f.cwd)
    _, body, _, code = prune._split_header(result or "")
    ok = None if result is None else _ok(code, body)
    low = name.lower()
    if low == "update_plan" and isinstance(c.args.get("plan"), list):
        f.plan = (turn, c.args)
        return
    rest, cwd = prune._strip_cd(c.cmd, c.cwd) if c.cmd is not None else ("", c.cwd)
    changes = _changes(c, rest, cwd)
    for p, kind, detail, text in changes:
        e = _file(f, p)
        e["changes"].append({"turn": turn, "kind": kind, "ok": ok, "detail": detail})
        if ok is not False:
            e["text"] = (turn, "as written", text, True) if text is not None else None
    for p in _reads(c, rest, cwd):
        e = _file(f, p)
        e["reads"] += 1
        e["last_read"] = turn
        whole = bool(c.read and c.read[1]) and not prune._TRUNCATED.search(body)
        if c.read and c.read[0] == p and ok is not False and body.strip() and (whole or not (e["text"] or (0,) * 4)[3]):
            how = f"`{_one_line(_cmd_line(rest), 100)}`" if c.cmd is not None else f"{name}"
            e["text"] = (turn, f"as shown by {how}", body, whole)  # a slice never replaces a whole current text
    changes = [x[:3] for x in changes]
    looks = c.cmd is not None and _only_looks(rest)
    excerpt = _outcome(body, False, 300, errors_only=looks) if ok is False else ""
    if ok is False and (excerpt or not looks):  # not: a grep that found nothing
        what = (
            f"`{_one_line(_cmd_line(rest), 160)}`"
            if c.cmd is not None and not changes
            else f"{name} on {', '.join(f.rel(p) for p, _, _ in changes)}"
            if changes
            else f"{name} {_one_line(json.dumps(c.args, ensure_ascii=False), 120)}"
        )
        f.failures.append(
            {
                "turn": turn,
                "what": what,
                "excerpt": excerpt,
                "key": _cmd_line(rest),
                "files": [p for p, _, _ in changes],
            }
        )
    if looks and not changes and not c.read and ok is not False and body.strip():
        line = _cmd_line(rest)
        f.looks.pop(line, None)
        f.looks[line] = (turn, body)
    if c.cmd is not None and (changes or looks) and low != "write_stdin":
        return  # a file change or a look around: the Files sections say it
    if c.cmd is None and (changes or c.read or low in prune._READ_TOOLS):
        return
    line = _cmd_line(rest) if c.cmd is not None else f"{name} {_one_line(json.dumps(c.args, ensure_ascii=False), 120)}"
    if not line.strip():
        return
    e = f.commands.pop(line, None) or {"turns": [], "failed_at": None}
    if ok is False:
        e["failed_at"] = turn
    e.update(turns=e["turns"] + [turn], code=code, ok=ok, out=_outcome(body, ok, 160) if result is not None else "")
    if result is not None and "Process running with session ID" in (result or "")[:300]:
        e["out"] = "still running when the next turn started"
    f.commands[line] = e  # re-inserted: the dict stays in order of each command's latest run


def _cmd_line(rest: str) -> str:
    rest = prune._ANY_HEREDOC.sub("<<EOF …", rest or "")
    lines = [x for x in rest.strip().split("\n") if x.strip()]
    return (lines[0] + (" …" if len(lines) > 1 else "")) if lines else ""


def _segments(rest: str) -> list[str]:
    """A command line split at unquoted && || ; | and newlines (a heredoc's text left out)."""
    rest = prune._ANY_HEREDOC.sub("<<EOF", rest or "")
    out, cur, q, i = [], [], None, 0
    while i < len(rest):
        ch = rest[i]
        if q:
            if ch == "\\" and q == '"' and i + 1 < len(rest):
                cur.append(rest[i : i + 2])
                i += 2
                continue
            q = None if ch == q else q
        elif ch in "'\"":
            q = ch
        elif ch == "\\" and i + 1 < len(rest):
            cur.append(rest[i : i + 2])
            i += 2
            continue
        elif ch in ";|\n" or (ch == "&" and rest[i - 1 : i] not in (">", "<") and rest[i + 1 : i + 2] != ">"):
            out.append("".join(cur))
            cur = []
            i += 2 if rest[i : i + 2] in ("&&", "||") else 1
            continue
        cur.append(ch)
        i += 1
    out.append("".join(cur))
    return [s.strip() for s in out if s.strip()]


_SHELL_KEYWORDS = {"do", "then", "else", "elif", "!", "time"}


def _only_looks(rest: str) -> bool:
    """Only reads and searches (cat, sed -n, rg, ls, git status/diff/log, echo, loops of those): no side effects."""
    segs = _segments(rest)
    for s in segs:
        w = s.split()
        while w and (w[0] in _SHELL_KEYWORDS or re.fullmatch(r"\w+=\S*", w[0])):
            w = w[1:]
        if not w or w[0] in ("for", "done", "fi", "while", "if", "cd", "{", "}", "esac"):
            continue
        if w[0] == "git" and len(w) > 1 and w[1] in _GIT_LOOK:
            continue
        if w[0] not in _LOOK_WORDS or (w[0] == "sed" and any(x.startswith("-i") for x in w[1:3])):
            return False
        if re.search(r"(?<![<\d&=>-])\d?>>?\s*(?!/dev/|&)[^\s>]", s):  # writes something somewhere
            return False
    return bool(segs)


def _outcome(body: str, ok, n: int, errors_only: bool = False) -> str:
    """One line of what a command printed: the first error line of a failure, else the last line."""
    lines = [x.strip() for x in (body or "").split("\n") if x.strip()]
    if not lines or lines == ["(no output)"]:
        return ""
    if ok is False:
        for k, x in enumerate(lines):
            if _ERR.search(x):
                return _one_line(" / ".join(lines[k : k + 2]), n)
        if errors_only:
            return ""
    return _one_line(lines[-1], n)


def _changes(c: prune.Call, rest: str, cwd: str | None) -> list[tuple[str, str, str, str | None]]:
    """(path, what happened to it, a short form of the change, the file's whole new text when the call carries it)
    for every file the call changes."""
    out: list = []
    a, low = c.args, c.name.lower()
    if low in prune._WRITE_TOOLS and c.full_writes:
        text = next((a[k] for k in ("content", "file_text", "text") if isinstance(a.get(k), str)), "")
        return [(c.full_writes[0], f"written whole ({text.count(chr(10)) + 1} lines)", _first_lines(text, "+"), text)]
    if low in prune._EDIT_TOOLS and c.writes:
        old, new = (
            str(a.get("old_string") or a.get("old_str") or ""),
            str(a.get("new_string") or a.get("new_str") or ""),
        )
        return [(c.writes[0], "edited", _one_line(f"-{old} +{new}", 200) if old or new else "", None)]
    if c.cmd is None:
        return [(p, "changed", "", None) for p in c.writes]
    if "*** Begin Patch" in rest:
        cur = None
        for line in rest.split("\n"):
            h = _PATCH_SEC.match(line)
            if h:
                kind = {"Add": "created", "Update": "edited", "Delete": "deleted"}[h.group(1)]
                cur = [prune._join(cwd, h.group(2)), kind, []]
                out.append(cur)
            elif line.startswith("*** Move to: ") and cur:
                cur[1] = f"moved to {line[13:].strip()}"
            elif cur and line[:1] in "+-" and not line.startswith(("+++", "---", "***")):
                cur[2].append(line)
        return [
            (p, k, _clip("\n".join(ch[:6]), 240), "\n".join(x[1:] for x in ch) if k == "created" else None)
            for p, k, ch in out
        ]
    full = {}
    for h in prune._HEREDOC.finditer(rest):
        full[prune._join(cwd, h.group("path"))] = h.group("body")
    # a `>` inside quotes (a sed or grep pattern, an HTML snippet) is not a redirection
    bare = re.sub(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", "''", prune._ANY_HEREDOC.sub("<<EOF", rest))
    real = {prune._join(cwd, w.group("path")) for w in prune._REDIRECT.finditer(bare)}
    for s in _segments(bare):
        w = s.split()
        if w and (w[0] == "tee" or (w[0] == "sed" and any(x.startswith("-i") for x in w[1:3]))) and len(w) > 1:
            real.add(prune._join(cwd, w[-1].strip("'\"")))
    for p in c.writes:
        if p not in full and p not in real:
            continue
        if p in full:
            out.append((p, f"written whole ({full[p].count(chr(10)) + 1} lines)", _first_lines(full[p], "+"), full[p]))
        else:
            out.append((p, "changed by a command", f"`{_one_line(_cmd_line(rest), 120)}`", None))
    return out


def _first_lines(text: str, mark: str) -> str:
    lines = [mark + x for x in text.split("\n")[:4]]
    return _clip("\n".join(lines), 200)


_MULTI_READ = re.compile(
    r"\A(?:cat|nl(?:\s+-ba)?|bat|wc(?:\s+-[lcw])?|head|tail|sed\s+-n\s+\S+)\s+(?P<rest>[^|;&<>]+)\Z"
)


def _reads(c: prune.Call, rest: str, cwd: str | None) -> list[str]:
    if c.read:
        return [c.read[0]]
    if c.cmd is None:
        return []
    out = []
    for s in re.split(r"&&|\|\||;|\||\n", prune._ANY_HEREDOC.sub("", rest)):
        r = _MULTI_READ.match(s.strip())
        if r:
            for w in r.group("rest").split():
                w = w.strip("'\"")
                if w and not w.startswith("-") and not w.isdigit() and ("." in w or "/" in w) and "*" not in w:
                    out.append(prune._join(cwd, w))
    return list(dict.fromkeys(out))


# ---- the brief ------------------------------------------------------------------------------------------------------------
def _fit(lines: list[str], cap: int, more: str = "earlier entries") -> list[str]:
    """The last lines that fit in cap characters, with a note of how many earlier ones were left out."""
    if sum(len(x) + 1 for x in lines) <= cap:
        return lines
    out, n = [], 0
    for x in reversed(lines):
        if n + len(x) + 1 > cap - 40 and out:
            break
        out.append(x if n + len(x) + 1 <= cap else _clip(x, max(80, cap - n - 40)))
        n += len(out[-1]) + 1
    out.reverse()
    gone = len(lines) - len(out)
    return ([f"- ({gone} {more} left out)"] if gone else []) + out


def _users(f: Facts, cap: int) -> list[str]:
    """Every user message, each clipped as little as the room allows (a constraint stated early counts as much as a
    late one); only when even 300 characters each is too much are the oldest left out."""
    if not f.users:
        return []
    per = 4000
    while True:
        lines = [f"- (before turn {t}) " + _clip(x, per).replace("\n", "\n  ") for t, x in f.users]
        if sum(len(x) + 1 for x in lines) <= cap or per <= 300:
            return _fit(lines, cap, "earlier messages")
        per = max(300, int(per * 0.75))


def _files(f: Facts, changed: list, cap: int) -> list[str]:
    """Every changed file with its state; the last change shown for as many of the newest as there is room for;
    only when even the one-line forms do not fit are the oldest left out."""
    full = [_file_line(f, p, e) for p, e in changed]
    if sum(len(x) + 1 for x in full) <= cap:
        return full
    out = [_file_line(f, p, e, detail=False) for p, e in changed]
    n = sum(len(x) + 1 for x in out)
    if n > cap:
        return _fit(out, cap, "files changed earlier")
    for k in range(len(out) - 1, -1, -1):  # newest first
        extra = len(full[k]) - len(out[k])
        if n + extra <= cap:
            out[k], n = full[k], n + extra
    return out


def _file_line(f: Facts, p: str, e: dict, detail: bool = True) -> str:
    ch = e["changes"]
    done = [x for x in ch if x["ok"] is not False]
    bits = []
    if done:
        last = done[-1]
        kinds = list(dict.fromkeys(x["kind"].split(" (")[0] for x in done))
        if last["kind"] == "deleted":
            bits.append(f"deleted at turn {last['turn']}")
        else:
            created = next((x for x in done if x["kind"] == "created" or x["kind"].startswith("written whole")), None)
            if created:
                bits.append(f"{created['kind']} at turn {created['turn']}")
            edits = [x for x in done if x is not created]
            if edits:
                bits.append(
                    f"{'/'.join(k for k in kinds if k not in ('created',))} {len(edits)}x, last at turn {edits[-1]['turn']}"
                )
    failed = [x for x in ch if x["ok"] is False]
    if failed:
        bits.append(f"{len(failed)} change(s) failed (last at turn {failed[-1]['turn']})")
    if e["reads"]:
        bits.append(f"read {e['reads']}x")
    line = f"- {f.rel(p)}: " + "; ".join(bits)
    detail = ((done[-1]["detail"] if done else "") or "") if detail else ""
    if detail:
        line += "; last change:\n    " + detail.replace("\n", "\n    ")
    return line


def _looks(f: Facts, cap: int) -> list[str]:
    """Whatever room is left after the file texts: the latest output of each search / listing, newest first."""
    have = list(reversed(f.looks.items()))
    out, left = [], cap
    for k, (line, (t, body)) in enumerate(have):
        room = min(3000, left // (len(have) - k) - 80 - len(line))
        if room < 200:
            break
        block = f"- (turn {t}) `{_one_line(line, 160)}`\n```\n{_clip(body, room)}\n```"
        out.append(block)
        left -= len(block) + 1
    return out


def _texts(f: Facts, changed: list, cap: int) -> list[str]:
    """Whatever room is left: the newest text the conversation shows of each file (a whole write, or a read after
    the last change), the changed files first, then those only read, newest first, each clipped to its share."""
    seen = sorted(
        ((p, e) for p, e in f.files.items() if not e["changes"] and e.get("text")), key=lambda pe: -pe[1]["text"][0]
    )
    have = [(p, e["text"]) for p, e in list(reversed(changed)) + seen if e.get("text") and e["text"][2].strip()]
    out, left = [], cap
    for k, (p, (t, how, text, _)) in enumerate(have):
        room = min(8000, left // (len(have) - k) - 80)
        if room < 300:
            break
        block = f"### {f.rel(p)} (turn {t}, {how})\n```\n{_clip(text, room)}\n```"
        out.append(block)
        left -= len(block) + 1
    return out


def sections(f: Facts, limit: int, narrative: tuple[str, str] | None = None, texts: bool = True) -> str:
    """The brief's body, at most `limit` characters. The core sections (the user's words, where it stood, the
    model's narrative, files changed, failures, plan, commands, the agent's notes, files read) share the limit by
    weight, and a section that needs less than its share gives the rest to the others; whatever is left after them
    goes to the latest text of files and the latest search output (`texts`; left out of the model's digest)."""
    T = f.turns
    changed = [(p, e) for p, e in f.files.items() if e["changes"]]
    changed.sort(key=lambda pe: pe[1]["changes"][-1]["turn"])
    read_only = [f.rel(p) for p, e in sorted(f.files.items(), key=lambda pe: pe[1]["last_read"]) if not e["changes"]]
    later_ok = {}
    for line, e in f.commands.items():
        if e["ok"] is not False:
            later_ok[line] = e["turns"][-1]
    fail_lines = []
    for x in f.failures:
        after = later_ok.get(x["key"])
        if x["files"]:
            ok_later = [
                c["turn"]
                for p in x["files"]
                for c in f.files[p]["changes"]
                if c["ok"] is not False and c["turn"] > x["turn"]
            ]
            after = min(ok_later) if len(ok_later) else None
        note = (
            f" (a later change to it worked, at turn {after})"
            if x["files"] and after
            else f" (ran fine later, at turn {after})"
            if after and after > x["turn"]
            else ""
        )
        fail_lines.append(f"- (turn {x['turn']}) {x['what']}{note}" + (f": {x['excerpt']}" if x["excerpt"] else ""))
    cmd_lines = []
    for line, e in f.commands.items():
        n = len(e["turns"])
        res = "" if e["ok"] is None else "ok" if e["ok"] else "FAILED"
        if e.get("code") is not None:
            res = f"exit {e['code']}"
        was = f", failed earlier at turn {e['failed_at']}" if e["failed_at"] and e["ok"] is not False else ""
        out = f": {e['out']}" if e.get("out") else ""
        cmd_lines.append(
            f"- (turn {e['turns'][-1]}{f', {n} runs' if n > 1 else ''}) `{_one_line(line, 160)}` -> {res or 'no result'}{was}{out}"
        )
    plan_lines = []
    if f.plan:
        t, a = f.plan
        if a.get("explanation"):
            plan_lines.append(_one_line(str(a["explanation"]), 300))
        for s in a.get("plan") or []:
            if isinstance(s, dict):
                plan_lines.append(f"- [{s.get('status', '?')}] {_one_line(str(s.get('step', '')), 200)}")
    note_lines = [
        f"- (turn {t}{', answer to the user' if final else ''}) " + _clip(x, 700).replace("\n", "\n  ")
        for t, x, final in f.notes
    ]
    last_note = f.notes[-1] if f.notes else None
    where = [f"- The {T} turn(s) summarised here are followed by the conversation from turn {T + 1} on, verbatim."]
    if last_note:
        where.insert(
            0,
            f"- The agent's last words before that (turn {last_note[0]}): "
            + _clip(last_note[1], 500).replace("\n", " "),
        )

    spec = [  # (key, title, weight, render(cap) -> lines)
        ("users", "## What the user asked for, in their own words (after the opening task above)", 3.0, lambda cap: _users(f, cap)),
        ("where", "## Where it stood", 0.6, lambda cap: _fit(where, cap)),
        ("narrative", f"## Summary of the work so far (written by {narrative[1]})" if narrative else "", 2.0,
         lambda cap: [_clip(narrative[0], cap)] if narrative else []),
        ("files", f"## Files changed (state at the end of turn {T})", 2.0, lambda cap: _files(f, changed, cap)),
        ("failures", "## What was tried and failed", 1.2, lambda cap: _fit(fail_lines, cap)),
        ("plan", f"## The agent's plan (update_plan at turn {f.plan[0]})" if f.plan else "", 0.6, lambda cap: _fit(plan_lines, cap)),
        ("commands", "## Commands that ran, latest run of each, with the outcome", 1.4, lambda cap: _fit(cmd_lines, cap, "earlier commands")),
        ("notes", "## The agent's own notes (most recent last)", 0.8 if narrative else 2.0, lambda cap: _fit(note_lines, cap, "earlier notes")),
        ("reads", "## Files read but not changed", 0.4, lambda cap: [_clip(", ".join(read_only), cap)] if read_only else []),
    ]  # fmt: skip
    fill = [  # only what the sections above leave, split between these
        ("texts", "## Latest text of files as last shown (after any change to them; changed files first, newest first)",
         lambda cap: _texts(f, changed, cap) if texts else []),
        ("looks", "## Output of the latest searches and listings (newest first)", lambda cap: _looks(f, cap) if texts else []),
    ]  # fmt: skip
    order = ["users", "narrative", "plan", "files", "failures", "commands", "notes", "reads", "texts", "looks", "where"]

    def section(title, render, cap):
        body = render(max(0, cap - len(title) - 1)) if title and cap > len(title) + 40 else []
        return (title + "\n" + "\n".join(body)) if body and any(x.strip() for x in body) else ""

    # what each section would take with no limit; then water-filling: a section that needs less than its weighted
    # share gets what it needs, the others split the rest by weight
    need = {k: len(x) + 48 if (x := section(t, r, limit)) else 0 for k, t, _, r in spec}  # + slack for rounding
    weight = {k: w for k, _, w, _ in spec}
    cap, left, active = {}, limit - 2 * len(spec), [k for k in need if need[k]]
    while active:
        total = sum(weight[k] for k in active)
        small = [k for k in active if need[k] <= left * weight[k] / total]
        if not small:
            cap.update({k: int(left * weight[k] / total) for k in active})
            break
        for k in small:
            cap[k] = need[k]
            left -= need[k]
            active.remove(k)
    done = {k: section(t, r, cap.get(k, 0)) for k, t, _, r in spec}
    left = limit - sum(len(x) + 2 for x in done.values() if x)
    for n, (k, t, r) in enumerate(fill):
        done[k] = section(t, r, left // (len(fill) - n) - 2)
        left -= len(done[k]) + 2 if done[k] else 0
    out = "\n\n".join(done[k] for k in order if done.get(k))
    return _clip(out, limit) if len(out) > limit else out


def brief(f: Facts, limit: int, first: int, last: int, chars: int, narrative: tuple[str, str] | None = None) -> str:
    head = (
        f"[Context note from provenance-core, not from the user: turns {first}-{last} of this conversation "
        f"({chars} characters of messages, tool calls and output) are condensed below so that the rest fits. Each "
        f"item happened as stated; the files may have changed since, so read a file again before relying on its "
        f"exact text. The conversation continues verbatim after this note.]"
    )
    return head + "\n\n" + sections(f, max(500, limit - len(head) - 2), narrative)


# ---- the model's part -----------------------------------------------------------------------------------------------------
SUMMARY_SYSTEM = """You condense the earlier part of a coding agent's work session so that the agent can carry on \
without it. You get a digest of that part: the user's messages, the files the agent changed, the commands it ran \
and their outcomes, and the agent's own notes. Write these three sections as short markdown bullet points:

## Decisions made
## What was tried and did not work
## In progress and the next step

Use only facts in the digest. Name files, functions, commands and errors exactly as they appear there. Do not \
repeat the user's messages or list every file. At most {n} characters in total. No preamble, no closing remarks."""


def _clean(text: str, n: int) -> str | None:
    t = re.sub(r"\A\s*```\w*\n|\n```\s*\Z", "", (text or "").strip()).strip()
    if len(t) < 80:
        return None
    if len(t) > n:
        cut = t[:n]
        t = cut[: cut.rfind("\n")] if "\n" in cut[n // 2 :] else cut
        t = t.rstrip() + "\n[… the rest of the model's summary was cut to its size bound …]"
    return t


# ---- the pass ---------------------------------------------------------------------------------------------------------------
class Compactor:
    """Computes what a cut's brief is made of and keeps it: the extracted facts in memory, the model's narrative (or
    the fact that there is none) in memory and, when a model wrote it, on disk under `home`/compact. Both are keyed by
    a hash of exactly the messages summarised (call ids aside) and the model options, so the same conversation, grown
    by a few turns, gets the same brief back until its cut moves; a restart reuses a model's narrative from disk."""

    def __init__(self, home: Path | str | None = None, entries: int = 64):
        """`entries`: cuts remembered in memory (their extracted facts, the biggest part: a quarter of that)."""
        self.mem: OrderedDict[str, dict] = OrderedDict()  # key -> {"narrative": [text, who] | None, "writer", ...}
        self.facts: OrderedDict[str, Facts] = OrderedDict()
        self.entries = entries
        self.home = Path(home) if home else None
        self.inflight: dict[str, asyncio.Future] = {}
        self.stats = {"hits": 0, "misses": 0, "model_calls": 0, "model_failures": 0}

    # ---- cache
    def _get(self, key: str) -> dict | None:
        v = self.mem.get(key)
        if v is None and self.home:
            try:
                v = json.loads((self.home / "compact" / f"{key}.json").read_text())
            except (OSError, ValueError):
                v = None
            if v is not None:
                self._put(key, v, disk=False)
        if v is not None:
            self.mem.move_to_end(key)
        return v

    def _put(self, key: str, v: dict, disk: bool):
        self.mem[key] = v
        self.mem.move_to_end(key)
        while len(self.mem) > max(1, self.entries):
            self.mem.popitem(last=False)
        if disk and self.home:
            d = self.home / "compact"
            try:
                d.mkdir(parents=True, exist_ok=True)
                tmp = d / f"{key}.tmp"
                tmp.write_text(json.dumps(v))
                tmp.replace(d / f"{key}.json")
                files = sorted(d.glob("*.json"), key=lambda x: x.stat().st_mtime)
                for old in files[: max(0, len(files) - 4 * max(1, self.entries))]:
                    old.unlink(missing_ok=True)
            except OSError:
                pass

    def _facts(self, p: dict) -> Facts:
        f = self.facts.get(p["key"])
        if f is None:
            f = self.facts[p["key"]] = extract(p["prefix"], p["opening"])
            while len(self.facts) > max(1, self.entries // 4):  # the biggest objects here: keep fewer
                self.facts.popitem(last=False)
        self.facts.move_to_end(p["key"])
        return f

    # ---- the parts of a compaction
    def plan(self, raw: list[dict], view: list[dict], budget: int, o: dict, measure=json_size) -> dict | None:
        """Where the cut goes and what is summarised; None when there is nothing to do (it fits, or too few turns).
        `raw`: the messages as the client sent them (the brief is made from these, so it depends on the summarised
        part alone); `view`: the same messages as they would otherwise be sent (pruned, maybe; same count and order).
        `use_room`: the brief may also take whatever the kept turns leave of the budget (for prompts that no cache
        has to match, i.e. a web chat's first message); otherwise its size is fixed by the budget alone."""
        before = sum(measure(m) for m in view)
        if before <= budget:
            return None
        # judged on the history as sent, which only ever grows at its end: so the cut only ever moves forward. (The
        # pruned view can shrink when prune's own boundary moves, which would pull the cut back and forth.)
        cut = boundary(raw, budget, o, measure)
        if not cut:
            return None
        b, opening, trim = cut
        ids = {tc.get("id") for m in raw[:b] for tc in m.get("tool_calls") or []}
        orphans = {i for i in range(b, len(raw)) if raw[i].get("role") == "tool" and raw[i].get("tool_call_id") in ids}
        prefix = raw[:b] + [raw[i] for i in sorted(orphans)]
        head = [view[i] for i in range(b) if i < opening or view[i].get("role") in ("system", "developer")]
        tail = [view[i] for i in range(b, len(view)) if i not in orphans]
        limit = brief_limit(budget, o)
        if o.get("use_room") and not trim:
            limit = max(limit, budget - sum(measure(m) for m in head + tail) - 64)
        writer = o.get("model") or None
        seed = [VERSION, writer, int(o["model_chars"]) if writer else 0, [_shape(m) for m in prefix]]
        key = hashlib.sha256(json.dumps(seed, sort_keys=True, default=str).encode()).hexdigest()[:40]
        return {
            "cut": b,
            "opening": opening,
            "trim": trim,
            "prefix": prefix,
            "head": head,
            "tail": tail,
            "limit": limit,
            "key": key,
            "budget": budget,
            "before": before,
            "chars": prune.size(prefix[opening:]),
        }

    def _assemble(self, p: dict, entry: dict, measure, t0: float, scope: str, cached: bool):
        f = self._facts(p)
        nar = tuple(entry["narrative"]) if entry.get("narrative") else None
        text = brief(f, p["limit"], 1, f.turns, p["chars"], nar)
        tail, trimmed = p["tail"], 0
        if p["trim"]:
            room = p["budget"] - sum(measure(m) for m in p["head"]) - measure({"role": "user", "content": text})
            tail, trimmed = _trim_tail(tail, room, measure)
        out = p["head"] + [{"role": "user", "content": text}] + tail
        rep = {
            "scope": scope,
            "before": p["before"],
            "after": sum(measure(m) for m in out),
            "budget": p["budget"],
            "boundary": p["cut"],
            "summarised": {"messages": len(p["prefix"]) - p["opening"], "turns": f.turns, "chars": p["chars"]},
            "kept": {"messages": len(tail), "turns": sum(1 for m in tail if m.get("role") == "assistant")},
            "brief_chars": len(text),
            "writer": entry.get("writer", "extractive"),
            "model_error": entry.get("error"),
            "cached": cached,
            "trimmed": trimmed,
            "ms": round((time.perf_counter() - t0) * 1000, 1),
            "key": p["key"][:12],
        }
        return out, rep

    def compact(self, raw, view, budget, o, measure=json_size, scope="api") -> tuple[list[dict], dict | None]:
        """Rules only, or with a model narrative already in the cache: never waits on anything."""
        t0 = time.perf_counter()
        p = self.plan(raw, view, budget, o, measure)
        if not p:
            return view, None
        got = self._get(p["key"])
        if got is not None:
            self.stats["hits"] += 1
            return self._assemble(p, got, measure, t0, scope, True)
        self.stats["misses"] += 1
        entry = {"narrative": None, "writer": "extractive"}
        if not self._wants_model(p, o, True):  # else leave the slot for the model's narrative (acompact)
            self._put(p["key"], entry, disk=False)
        return self._assemble(p, entry, measure, t0, scope, False)

    async def acompact(
        self, raw, view, budget, o, measure=json_size, scope="api", summarize=None
    ) -> tuple[list[dict], dict | None]:
        """As compact(), and when `model` is set and the part summarised is big enough, a model writes the narrative
        first (bounded by model_timeout_s; on any failure the rules-only brief). The outcome is cached under the cut's
        key and reused for every later request with the same cut, so their prefix stays byte-identical."""
        t0 = time.perf_counter()
        p = self.plan(raw, view, budget, o, measure)
        if not p:
            return view, None
        got = self._get(p["key"])
        if got is not None:
            self.stats["hits"] += 1
            return self._assemble(p, got, measure, t0, scope, True)
        if p["key"] in self.inflight:  # another request is making this one: share it
            got = await asyncio.shield(self.inflight[p["key"]])
            return self._assemble(p, got, measure, t0, scope, True)
        self.stats["misses"] += 1
        fut = asyncio.get_running_loop().create_future()
        self.inflight[p["key"]] = fut
        try:
            entry = await self._narrate(p, o, summarize) if self._wants_model(p, o, summarize) else None
            entry = entry or {"narrative": None, "writer": "extractive"}
            self._put(p["key"], entry, disk=bool(entry.get("narrative")) and bool(o.get("disk")))
            fut.set_result(entry)
        except BaseException as e:
            fut.set_exception(e)
            fut.exception()  # retrieved: no warning when nobody else was waiting
            raise
        finally:
            self.inflight.pop(p["key"], None)
        return self._assemble(p, entry, measure, t0, scope, False)

    @staticmethod
    def _wants_model(p: dict, o: dict, summarize) -> bool:
        return bool(o.get("model") and summarize and p["chars"] >= int(o["model_min_chars"]))

    async def _narrate(self, p: dict, o: dict, summarize) -> dict:
        """The model's narrative from the digest of the facts (never the raw history), or the reason there is none."""
        digest = sections(self._facts(p), int(o["model_input_chars"]), texts=False)
        n = int(o["model_chars"])
        t1 = time.perf_counter()
        self.stats["model_calls"] += 1
        try:
            text, who = await asyncio.wait_for(
                summarize(SUMMARY_SYSTEM.format(n=n), digest, str(o["model"]), n), float(o["model_timeout_s"])
            )
            text = _clean(text, n)
            if not text:
                raise ValueError("the model's summary was empty or too short")
        except Exception as e:  # noqa: BLE001 - a timeout, no capacity, a provider error: the rules-only brief
            self.stats["model_failures"] += 1
            return {
                "narrative": None,
                "writer": "extractive",
                "error": f"{type(e).__name__}: {str(e)[:160]}".rstrip(": "),
            }
        ms = round((time.perf_counter() - t1) * 1000)
        return {"narrative": [text, who], "writer": f"model:{who}", "model_ms": ms}


def _trim_tail(tail: list[dict], room: int, measure) -> tuple[list[dict], int]:
    """Shorten the kept turns' tool output (oldest first, then harder) until they fit in `room`; their calls and
    every message stay. -> (the messages, how many were shortened)."""
    out = [dict(m) for m in tail]
    n = 0
    for cap in (4000, 1500, 600):
        for m in out:
            if sum(measure(x) for x in out) <= room:
                return out, n
            t = m.get("content")
            if m.get("role") == "tool" and isinstance(t, str) and len(t) > cap:
                m["content"] = (
                    t[: cap * 2 // 3]
                    + f"\n[… {len(t) - cap} characters of this output omitted to fit …]\n"
                    + t[-(cap // 3) :]
                )
                n += 1
    return out, n


def summary(report: dict | None) -> str:
    """For a log line: '412345 -> 58000 chars: turns 1-46 (180 messages) in a 15000-char brief (extractive, 12 ms,
    cached), 40 messages kept'."""
    if not report:
        return "nothing to compact"
    s = report["summarised"]
    how = report["writer"] + (", cached" if report["cached"] else "") + f", {report['ms']:.0f} ms"
    if report.get("model_error"):
        how += f", model failed: {report['model_error']}"
    return (
        f"{report['before']} -> {report['after']} chars: turns 1-{s['turns']} ({s['messages']} messages) in a "
        f"{report['brief_chars']}-char brief ({how}), {report['kept']['messages']} messages kept"
        + (f", {report['trimmed']} kept outputs shortened" if report["trimmed"] else "")
    )
