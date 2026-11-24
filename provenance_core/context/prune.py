"""History pruning: a rule-based pass that makes an agent's conversation smaller without changing what the model
needs to know. No model calls, no network: a pure function of the messages, so the same history always prunes to the
same thing.

Coding agents resend the whole conversation on every request. Most of its weight is tool results, and much of that is
dead by the time it is sent again: a file read that was read again later, the same `git status` twice, a file written
in full and then rewritten, the header lines the harness puts on every command result. The rules (each can be turned
off by leaving it out of `rules`):

- dup_system      a block of instructions repeated in the system/developer messages (Codex can send the same 20k
                  developer message once per turn): later copies go, the first stays
- exec_header     the bookkeeping on an old command result ("Chunk ID", "Wall time", "Original token count",
                  "Output:") becomes "Exit code: N"; a still-running process keeps its session id
- stale_read      an old read of one file (cat / sed -n / nl / head / tail, or a Read tool), when the file was
                  changed afterwards by a call that succeeded and then read in full again: a one-line stub
- dup_output      an old tool result identical to a later one (the same grep, ls, git status, test failure): a stub
                  that says so
- repeated_lines  runs of lines in an old tool result that appear again verbatim in a later tool result (the same
                  file or slice read twice, a superset read later): each run becomes a one-line marker
- old_write       an old whole-file write (a `cat > f <<EOF` heredoc, an apply_patch "Add File", a Write tool) whose
                  file was later written whole or read whole again: the file text in the call goes, the call and its
                  path stay
- write_echo      bulky success output of an old write/patch call: the summary lines stay, the rest goes
- long_output     an old, long result of a command that is not a file viewer (build and test logs, listings, curl,
                  search results): head and tail stay
- filler          old progress narration on a tool-calling turn ("Let me look at the tests."), recognised
                  conservatively: the call itself says the same

Never touched: user messages, the last `keep_turns` assistant turns and everything after them, tool call ids and
names, and the number, order and roles of messages. Every assistant tool call keeps its tool result; only contents
shrink. "Appears again later" is only ever judged against content that is itself kept, so a stub always points at
something the model can still see.

Two modes, for the two places the pass runs (docs/TECHNICAL.md, "History pruning"):
- "fresh": everything before the last `keep_turns` assistant turns may be pruned, with the whole history as
  evidence. For a prompt that starts a new chat (the web bridge's first message), where nothing has to line up with
  an earlier request.
- "stable": only the part before an epoch boundary is pruned, with evidence from that part alone. The boundary sits
  on every `step`-th assistant turn and moves only after `step` more turns, so between moves every request starts with
  the same pruned history, byte for byte (provider prompt caches keep hitting), and the pruned part is a function of
  that part alone, never of what comes after it."""

from __future__ import annotations

import json
import posixpath
import re

from ..settings import scoped

RULES = (
    "dup_system",
    "exec_header",
    "stale_read",
    "dup_output",
    "repeated_lines",
    "old_write",
    "write_echo",
    "long_output",
    "filler",
)

DEFAULTS = {
    "keep_turns": 4,  # the last 4 assistant turns and what follows them are left exactly as sent
    "step": 8,  # stable mode: the pruned boundary moves every 8 assistant turns
    "min_dup_chars": 120,  # dup_output: a smaller identical result is not worth a stub
    "run_lines": 4,  # repeated_lines: lines are matched in windows of this many consecutive lines
    "min_run_chars": 200,  # a repeated run shorter than this stays
    "long_chars": 6000,  # long_output: a non-viewer result longer than this keeps only its head and tail
    "long_head": 2500,
    "long_tail": 1500,
    "rules": list(RULES),
}


# ---- settings ---------------------------------------------------------------------------------------------------------
def config(scope: str, value) -> dict | None:
    """The options for one place the pass runs ("api": requests to API models, "web": the web bridge's fresh
    prompts), or None when it is off there. `value` is the setting as written: true / false, a dict of options, or
    {"api": ..., "web": ...} (each true / false / options). Off by default. PCORE_PRUNE overrides it: 0 / off, 1 / on /
    all, or the name of one scope."""
    return scoped("PRUNE", scope, value, DEFAULTS)


# ---- the parts of a message ---------------------------------------------------------------------------------------------
def _str(content) -> str | None:
    """Plain-string content, or None for anything else (content parts, which may hold images, are left alone)."""
    if content is None:
        return ""
    return content if isinstance(content, str) else None


def size(messages: list[dict]) -> int:
    """Characters of content and tool call arguments: what the pass can make smaller."""
    n = 0
    for m in messages:
        c = m.get("content")
        n += len(c) if isinstance(c, str) else len(json.dumps(c, ensure_ascii=False)) if c else 0
        for tc in m.get("tool_calls") or []:
            n += len((tc.get("function") or {}).get("arguments") or "")
    return n


# Codex's exec_command result ("Chunk ID: …\nWall time: …\nProcess exited with code 0\nOriginal token count: …\n
# Output:\n") and the stock shell tool's ("Exit code: 0\nWall time: … seconds\nOutput:\n")
_HEADER = re.compile(
    r"\A(?:Chunk ID: \S+\n)?Wall time: [^\n]*\n"
    r"(?:Process exited with code (?P<code>-?\d+)\n|(?P<running>Process running with session ID \d+)\n)?"
    r"(?:Original token count: \d+\n)?Output:\n?"
    r"|\AExit code: (?P<code2>-?\d+)\nWall time: [^\n]*\nOutput:\n?"
)


def _split_header(text: str) -> tuple[str, str, str | None, str | None]:
    """A command result -> (header, body, the header made short, exit code); ("", text, None, None) without one."""
    h = _HEADER.match(text)
    if not h:
        return "", text, None, None
    code = h.group("code") or h.group("code2")
    short = f"Exit code: {code}\n" if code is not None else (h.group("running") + "\n" if h.group("running") else "")
    return h.group(0), text[h.end() :], short, code


def _succeeded(code: str | None, body: str) -> bool:
    if code is not None:
        return code == "0"
    head = body.lstrip()[:200].lower()
    return head.startswith("success") or not re.search(r"\b(error|failed|failure|invalid|not found|denied)\b", head)


# ---- what a call does: commands, reads, writes ----------------------------------------------------------------------
_SHELLS = ("bash", "sh", "zsh", "/bin/bash", "/bin/sh", "/bin/zsh")
_PATH = r"""(?P<q>['"]?)(?P<path>(?!-)[^\s'"*?$`|;&<>(){}\[\]]+)(?P=q)"""
# a harness's mark of a cut output (Codex: "…1234 chars truncated…"): such a read did not show the whole file
_TRUNCATED = re.compile(r"(?:chars|tokens|lines|bytes) truncated|Warning: truncated output|\bomitted\b")
_QUIET = r"(?:\s+2>\s*/dev/null)?"
_FULL_READ = re.compile(rf"\A(?:cat|bat|cat -n|nl|nl -ba|nl -b a)\s+{_PATH}{_QUIET}\s*\Z")
_RANGE_READ = re.compile(
    rf"""\A(?:sed -n\s+['"]?\d+(?:,\d+)?p['"]?|head(?:\s+-n)?\s+-?\d+|tail(?:\s+-n)?\s+[-+]?\d+|head|tail)"""
    rf"""\s+{_PATH}{_QUIET}\s*\Z"""
)
# `cat > f <<'EOF'` ... `EOF` (or tee) at the start of a command: the file's whole new text
_HEREDOC = re.compile(
    rf"""(?:\A|(?<=[\n;&|]))[ \t]*(?:cat\s*>\s*|tee\s+){_PATH}\s*<<-?\s*(?P<eq>['"]?)(?P<tag>\w+)(?P=eq)[^\n]*\n"""
    r"""(?P<body>.*?)\n[ \t]*(?P=tag)[ \t]*(?=\n|\Z)""",
    re.S,
)
# any heredoc's body (a script fed to python, a file's text): not part of the command line
_ANY_HEREDOC = re.compile(
    r"""<<-?\s*(?P<eq>['"]?)(?P<tag>\w+)(?P=eq)[^\n]*\n.*?\n[ \t]*(?P=tag)[ \t]*(?=\n|\Z)""", re.S
)
_REDIRECT = re.compile(rf"(?<![<\d&=>-])\d?>>?\s*{_PATH}")
_PATCH_FILE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$", re.M)
_VIEW_WORDS = {"cat", "sed", "nl", "head", "tail", "bat", "echo", "printf", "wc"}
_READ_TOOLS = ("read", "read_file", "view")
_WRITE_TOOLS = ("write", "write_file", "create_file")
_EDIT_TOOLS = ("edit", "multiedit", "str_replace_editor", "edit_file")


def _command(name: str, args: dict) -> tuple[str | None, str | None]:
    """(the shell command a call runs, its working directory); (None, None) when it isn't a shell call."""
    cwd = args.get("workdir") or args.get("working_directory") or args.get("cwd")
    cmd = None
    for k in ("cmd", "command", "script"):
        v = args.get(k)
        if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            cmd = v[-1] if len(v) >= 3 and v[0] in _SHELLS and v[1] in ("-lc", "-c") else " ".join(v)
        elif isinstance(v, str):
            cmd = v
        if cmd is not None:
            break
    if cmd is None and name == "apply_patch" and isinstance(args.get("input"), str):
        cmd = "apply_patch <<'EOF'\n" + args["input"] + "\nEOF"
    return cmd, cwd if isinstance(cwd, str) else None


def _strip_cd(cmd: str, cwd: str | None) -> tuple[str, str | None]:
    """'cd DIR && rest' -> ('rest', DIR joined onto cwd)."""
    while True:
        m = re.match(r"\A\s*cd\s+(?P<q>['\"]?)(?P<d>[^\s'\";&|]+)(?P=q)\s*(?:&&|;)\s*", cmd)
        if not m:
            return cmd.strip(), cwd
        cwd, cmd = _join(cwd, m.group("d")), cmd[m.end() :]


def _join(cwd: str | None, path: str) -> str:
    if not path.startswith(("/", "~")) and cwd:
        path = posixpath.join(cwd, path)
    return posixpath.normpath(path)


class Call:
    """What one tool call reads and writes, as far as the call alone tells."""

    def __init__(self, name: str, arguments, cwd: str | None = None):
        """`cwd`: the session's working directory, for relative paths in calls that don't name one."""
        self.name = name or ""
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
        except ValueError:
            args = {}
        self.args = args if isinstance(args, dict) else {}
        self.cmd: str | None = None  # the shell command, for shell tools
        self.cwd: str | None = None
        self.read: tuple[str, bool] | None = None  # (path, whole file?)
        self.writes: list[str] = []  # paths it changes
        self.full_writes: list[str] = []  # paths whose whole text it carries (or deletes)
        self.viewer = False  # only shows text (cat / sed / echo, a Read tool): long_output leaves it alone
        low, a = self.name.lower(), self.args
        path = a.get("file_path") or a.get("path")
        if low in _READ_TOOLS + _WRITE_TOOLS + _EDIT_TOOLS:
            if isinstance(path, str):
                p = _join(cwd, path)
                if low in _READ_TOOLS:
                    whole = not any(a.get(k) for k in ("offset", "limit", "view_range", "start_line", "end_line"))
                    self.read, self.viewer = (p, whole), True
                else:
                    self.writes = [p]
                    self.full_writes = [p] if low in _WRITE_TOOLS else []
            return
        self.cmd, self.cwd = _command(self.name, a)
        if self.cmd is None:
            return
        self.cwd = _join(cwd, self.cwd) if self.cwd else cwd
        rest, cwd = _strip_cd(self.cmd, self.cwd)
        if "*** Begin Patch" in rest and "apply_patch" in rest[:200]:
            for m in _PATCH_FILE.finditer(rest):
                p = _join(cwd, m.group(2) or m.group(3))
                self.writes.append(p)
                if m.group(1) == "Add":
                    self.full_writes.append(p)
            return
        for h in _HEREDOC.finditer(rest):
            p = _join(cwd, h.group("path"))
            self.writes.append(p)
            self.full_writes.append(p)
        r = _FULL_READ.match(rest) or _RANGE_READ.match(rest)
        if r:
            self.read = (_join(cwd, r.group("path")), r.re is _FULL_READ)
        rest = _ANY_HEREDOC.sub("<<EOF", rest)  # a heredoc's text is data, not commands or redirections
        segs = [s.strip() for s in re.split(r"&&|\|\||;|\||\n", rest) if s.strip()]
        for s in segs:
            words = s.split()
            if words[0] == "sed" and any(w.startswith("-i") for w in words[1:3]) and len(words) > 2:
                self.writes.append(_join(cwd, words[-1].strip("'\"")))
            elif words[0] == "tee" and len(words) > 1:
                self.writes.append(_join(cwd, words[-1].strip("'\"")))
        for w in _REDIRECT.finditer(rest):
            if not w.group("path").startswith(("/dev/", "&")):
                self.writes.append(_join(cwd, w.group("path")))
        self.writes = list(dict.fromkeys(self.writes))
        # shows a file's text somewhere in it (`echo == && sed -n 1,80p f && rg x`): long_output leaves it alone
        self.viewer = bool(segs) and (
            any(_FULL_READ.match(s) or _RANGE_READ.match(s) for s in segs)
            or all(s.split()[0] in _VIEW_WORDS for s in segs)
            and not self.writes
        )


# ---- markers ------------------------------------------------------------------------------------------------------------
_MARK = "[… "  # every marker this pass writes starts like this and ends with " …]"


def _is_marker(line: str) -> bool:
    return line.startswith(_MARK) and line.endswith(" …]")


def _lines_marker(n: int) -> str:
    return f"[… {n} line{'s' if n != 1 else ''} omitted: repeated in a later tool result …]"


def _stub_dup(name: str) -> str:
    return f"[… same output as a later {name} call, omitted here …]"


def _stub_stale(path: str) -> str:
    return f"[… earlier read of {path} omitted: the file was changed afterwards and its new text is shown later …]"


# ---- the pass -----------------------------------------------------------------------------------------------------------
class Report:
    """What was removed: per rule, how many contents it changed and how many characters that saved."""

    def __init__(self, mode: str):
        self.mode, self.rules, self.before, self.after, self.boundary = mode, {}, 0, 0, 0

    def add(self, rule: str, before: int, after: int):
        if after < before:
            r = self.rules.setdefault(rule, {"count": 0, "saved": 0})
            r["count"] += 1
            r["saved"] += before - after

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "before": self.before,
            "after": self.after,
            "saved": self.before - self.after,
            "boundary": self.boundary,
            "rules": {k: self.rules[k] for k in RULES if k in self.rules},
        }


def summary(report: dict | None) -> str:
    """For a log line: 'saved 41230 of 265182 chars (dup_output 3/4200, repeated_lines 12/30000)'."""
    if not report or not report.get("saved"):
        return "nothing to prune"
    rules = ", ".join(f"{k} {v['count']}/{v['saved']}" for k, v in report["rules"].items())
    return f"saved {report['saved']} of {report['before']} chars ({rules})"


def prune_body(body: dict, mode: str = "fresh", options: dict | None = None) -> tuple[dict, dict]:
    """An OpenAI chat body with its messages pruned (a new dict; the input is left as it is) and the report."""
    messages, report = prune_messages(body.get("messages") or [], mode, options)
    return {**body, "messages": messages}, report


def boundary(messages: list[dict], mode: str, options: dict | None = None) -> int:
    """Index of the first message the pass leaves alone (0: nothing before it is old enough to prune)."""
    o = {**DEFAULTS, **(options or {})}
    asst = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
    keep = max(1, int(o["keep_turns"]))
    if len(asst) <= keep:
        return 0
    if mode == "stable":
        epochs = (len(asst) - keep) // max(1, int(o["step"]))
        return asst[epochs * max(1, int(o["step"]))] if epochs else 0
    return asst[-keep]


def prune_messages(messages: list[dict], mode: str = "fresh", options: dict | None = None) -> tuple[list[dict], dict]:
    """-> (the pruned messages: new dicts, the same count, order, roles and tool call ids; the report)."""
    if mode not in ("fresh", "stable"):
        raise ValueError(f"unknown prune mode {mode!r}")
    o = {**DEFAULTS, **(options or {})}
    rules = set(o["rules"])
    rep = Report(mode)
    out = [dict(m) for m in messages]
    rep.before = size(messages)
    if "dup_system" in rules:
        _dup_system(out, rep)
    cut = rep.boundary = boundary(out, mode, o)
    if cut:
        # stable: pruned as if the history ended at the boundary, so the result depends on that prefix alone
        _prune(out, cut, cut if mode == "stable" else len(out), o, rules, rep)
    rep.after = size(out)
    return out, rep.as_dict()


def _dup_system(out: list[dict], rep: Report):
    """Paragraphs of system/developer text given before: each run of them worth 500+ characters goes. Keeps the
    first copy, so a message's result depends only on what precedes it."""
    seen: set[str] = set()
    for m in out:
        if m.get("role") not in ("system", "developer"):
            continue
        text = _str(m.get("content"))
        if not text:
            continue
        paras = text.split("\n\n")
        again = []
        for p in paras:
            again.append(bool(p.strip()) and p.strip() in seen)
            seen.add(p.strip())
        if not any(again):
            continue
        res, i = [], 0
        while i < len(paras):
            if not again[i]:
                res.append(paras[i])
                i += 1
                continue
            j = i
            while j < len(paras) and (again[j] or not paras[j].strip()):
                j += 1
            while not paras[j - 1].strip():  # a trailing blank paragraph is not part of the run
                j -= 1
            run = "\n\n".join(paras[i:j])
            if len(run) >= 500:
                res.append(f"[… {len(run)} characters repeating instructions given above omitted …]")
            else:
                res += paras[i:j]
            i = j
        new = "\n\n".join(res)
        rep.add("dup_system", len(text), len(new))
        m["content"] = new


class _Evidence:
    """What the later, kept tool results show: whole bodies, windows of consecutive lines, whole-file reads and
    whole-file writes (by message index), and the files changed and then read whole again."""

    def __init__(self, k: int):
        self.k = k
        self.bodies: set[str] = set()
        self.windows: set[str] = set()
        self.full_reads: dict[str, list[int]] = {}
        self.full_writes: dict[str, list[int]] = {}
        self.stale: set[str] = set()

    def add_text(self, body: str):
        """The lines of a kept body; markers split it (lines on either side of one were not adjacent)."""
        seg: list[str] = []
        for line in body.split("\n") + [_MARK + " …]"]:
            if _is_marker(line):
                for s in range(len(seg) - self.k + 1):
                    self.windows.add("\n".join(seg[s : s + self.k]))
                seg = []
            else:
                seg.append(line)

    def covered(self, lines: list[str]) -> list[bool]:
        out = [False] * len(lines)
        for s in range(len(lines) - self.k + 1):
            if "\n".join(lines[s : s + self.k]) in self.windows:
                for j in range(s, s + self.k):
                    out[j] = True
        return out


def _session_cwd(messages: list[dict]) -> str | None:
    """The working directory a Codex session states in its opening environment message (in either the client's
    form, <cwd>…</cwd>, or the web bridge's lean one, "cwd: …"); it comes before any turn, so it never changes."""
    for m in messages:
        if m.get("role") == "assistant":
            return None
        text = _str(m.get("content")) or ""
        if m.get("role") == "user" and "<environment_context>" in text:
            got = re.search(r"<cwd>\s*(.*?)\s*</cwd>|cwd: ([^,;<\n]+)", text)
            if got:
                return (got.group(1) or got.group(2)).strip()
    return None


def _prune(out: list[dict], cut: int, end: int, o: dict, rules: set[str], rep: Report):
    """Prune out[:cut] in place, with evidence from out[:end] (end >= cut). Walks backwards, so whatever an earlier
    message is compared with is already in its final, kept form."""
    calls: dict[str, Call] = {}
    result_at: dict[str, int] = {}
    cwd = _session_cwd(out)
    for i, m in enumerate(out[:end]):
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            calls[tc.get("id")] = Call(fn.get("name", ""), fn.get("arguments"), cwd)
        if m.get("role") == "tool":
            result_at.setdefault(m.get("tool_call_id"), i)
    ev = _Evidence(max(2, int(o["run_lines"])))
    for i in range(end - 1, -1, -1):
        m = out[i]
        if m.get("role") == "tool":
            call = calls.get(m.get("tool_call_id")) or Call(m.get("name", ""), {})
            _tool(m, i, i < cut, call, ev, o, rules, rep)
        elif m.get("role") == "assistant" and i < cut and m.get("tool_calls"):
            if "old_write" in rules:
                _old_write(m, result_at, calls, ev, rep)
            if "filler" in rules:
                _filler(m, rep)


def _tool(m: dict, i: int, prunable: bool, call: Call, ev: _Evidence, o: dict, rules: set[str], rep: Report):
    text = _str(m.get("content"))
    if text is None:
        return
    header, body, short, code = _split_header(text)
    new_header, new_body, stub = header, body, False
    if prunable:
        name = call.name or "tool"
        if "exec_header" in rules and short is not None and short != header:
            new_header = short
            rep.add("exec_header", len(header), len(short))
        if "stale_read" in rules and call.read and call.read[0] in ev.stale and body.strip():
            new_body, stub = _stub_stale(call.read[0]), True
            rep.add("stale_read", len(body), len(new_body))
        elif "dup_output" in rules and len(body) >= int(o["min_dup_chars"]) and body in ev.bodies:
            new_body, stub = _stub_dup(name), True
            rep.add("dup_output", len(body), len(new_body))
        else:
            if "repeated_lines" in rules:
                new_body = _repeated_lines(new_body, ev, int(o["min_run_chars"]))
                rep.add("repeated_lines", len(body), len(new_body))
            if "write_echo" in rules and call.writes and len(new_body) > 300 and _succeeded(code, body):
                before, new_body = new_body, _write_echo(new_body)
                rep.add("write_echo", len(before), len(new_body))
            if "long_output" in rules and not call.viewer and len(new_body) > int(o["long_chars"]):
                before, new_body = new_body, _head_tail(new_body, int(o["long_head"]), int(o["long_tail"]))
                rep.add("long_output", len(before), len(new_body))
        if new_header != header or new_body != body:
            m["content"] = new_header + new_body
    # what this result leaves for the earlier ones to be compared with
    if not stub:
        ev.add_text(new_body)
        if new_body == body:
            ev.bodies.add(body)
            if call.read and call.read[1] and not _TRUNCATED.search(body):
                ev.full_reads.setdefault(call.read[0], []).append(i)
    if call.writes and _succeeded(code, body):
        # changed here and shown whole afterwards (read in full later, or this call carries the whole new text):
        # the reads before this are stale
        for p in call.writes:
            if ev.full_reads.get(p) or p in call.full_writes:
                ev.stale.add(p)
        for p in call.full_writes:
            ev.full_writes.setdefault(p, []).append(i)


def _repeated_lines(body: str, ev: _Evidence, min_chars: int) -> str:
    lines = body.split("\n")
    if len(lines) < ev.k:
        return body
    covered = ev.covered(lines)
    if not any(covered):
        return body
    res, j = [], 0
    while j < len(lines):
        if not covered[j]:
            res.append(lines[j])
            j += 1
            continue
        e = j
        while e < len(lines) and covered[e]:
            e += 1
        if sum(len(x) + 1 for x in lines[j:e]) >= min_chars:
            res.append(_lines_marker(e - j))
        else:
            res += lines[j:e]
        j = e
    return "\n".join(res)


def _write_echo(body: str) -> str:
    lines = body.split("\n")
    kept = [x for x in lines if re.match(r"\s*(Success|Done|Updated|Wrote|Created|Deleted|[AMD] \S)", x)][:20]
    if not kept or len("\n".join(kept)) > len(body) - 150:
        return body
    return "\n".join(kept) + f"\n[… {len(lines) - len(kept)} more lines of the write's output omitted …]"


def _head_tail(body: str, head_n: int, tail_n: int) -> str:
    """Head and tail at line boundaries; the body as it is when that would not save much."""
    head = body[:head_n]
    head = head[: head.rfind("\n") + 1] if "\n" in head else head
    tail = body[len(body) - tail_n :]
    tail = tail[tail.find("\n") + 1 :] if "\n" in tail else tail
    gone = body[len(head) : len(body) - len(tail)]
    if len(gone) < 1000:
        return body
    return head + f"[… {gone.count(chr(10)) + 1} lines ({len(gone)} characters) of old output omitted …]\n" + tail


def _old_write(m: dict, result_at: dict, calls: dict, ev: _Evidence, rep: Report):
    """The file text in an old whole-file write, when the same file was written whole or read whole later."""
    new_calls, changed = [], False
    for tc in m["tool_calls"]:
        call, at = calls.get(tc.get("id")), result_at.get(tc.get("id"))
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if call and at is not None and call.full_writes and isinstance(args, str):

            def later(p, at=at):
                return any(x > at for x in ev.full_reads.get(p, []) + ev.full_writes.get(p, []))

            new_args = _drop_write_body(call, later)
            if new_args is not None and len(new_args) < len(args):
                rep.add("old_write", len(args), len(new_args))
                tc, changed = {**tc, "function": {**fn, "arguments": new_args}}, True
        new_calls.append(tc)
    if changed:
        m["tool_calls"] = new_calls


_WRITE_NOTE = "[… {n} lines of file content omitted: {p} was written or read whole again later …]"


def _drop_write_body(call: Call, later) -> str | None:
    """The call's arguments (JSON) with the superseded file text replaced by a note; None when nothing changes."""
    a = call.args
    if call.name.lower() in _WRITE_TOOLS:
        p = call.full_writes[0]
        key = next((k for k in ("content", "file_text", "text") if isinstance(a.get(k), str)), None)
        if not key or a[key].count("\n") < 3 or not later(p):
            return None
        return json.dumps({**a, key: _WRITE_NOTE.format(n=a[key].count("\n") + 1, p=p)}, ensure_ascii=False)
    key = next((k for k in ("cmd", "command", "script", "input") if k in a), None)
    if key is None:
        return None
    v = a[key]
    shell_list = isinstance(v, list) and len(v) >= 3 and v[0] in _SHELLS
    text = v[-1] if shell_list else v
    if not isinstance(text, str):
        return None
    rest, cwd = _strip_cd(text, call.cwd)
    if "*** Begin Patch" in text:
        lines, res, j, changed = text.split("\n"), [], 0, False
        while j < len(lines):
            res.append(lines[j])
            hm = re.match(r"^\*\*\* Add File: (.+?)\s*$", lines[j])
            j += 1
            if hm:
                p = _join(cwd, hm.group(1))
                e = j
                while e < len(lines) and lines[e].startswith("+"):
                    e += 1
                if e - j > 3 and later(p):
                    res.append("+" + _WRITE_NOTE.format(n=e - j, p=p))
                    j, changed = e, True
        if not changed:
            return None
        text = "\n".join(res)
    else:
        at = text.rfind(rest)  # rest is the command without its `cd ... &&` prefix and outer whitespace
        pieces, last = [], 0
        for h in _HEREDOC.finditer(rest):
            p, body = _join(cwd, h.group("path")), h.group("body")
            if body.count("\n") >= 3 and later(p):
                note = _WRITE_NOTE.format(n=body.count("\n") + 1, p=p)
                pieces += [rest[last : h.start("body")], note]
                last = h.end("body")
        if not pieces:
            return None
        text = text[:at] + "".join(pieces) + rest[last:] + text[at + len(rest) :]
    return json.dumps({**a, key: v[:-1] + [text] if shell_list else text}, ensure_ascii=False)


_FILLER = re.compile(
    r"\A(?:(?:ok(?:ay)?|great|good|alright|now|next|first|then)[,.!]?\s+)?"
    r"(?:let me|let's|i'll|i will|i'm going to|i am going to|time to|checking|looking|reading|searching|running|"
    r"inspecting|opening|viewing)\b",
    re.I,
)
_SUBSTANCE = re.compile(
    r"\b(because|but|however|instead|although|though|since|so that|which|means|bug|error|fail|broke|wrong|issue|"
    r"problem|found|decid|must|should|need|note|important|cause|fix|conclu|seems|appears|looks like|turns out|"
    r"not|n't|done|works|passed)",
    re.I,
)


def _filler(m: dict, rep: Report):
    """Old one-line narration on a turn that calls tools ("Let me check the config."): the calls say the same."""
    text = _str(m.get("content"))
    if not text or not text.strip():
        return
    t = text.strip()
    if len(t) > 200 or "\n" in t or "?" in t or "`" in t or not _FILLER.match(t) or _SUBSTANCE.search(t):
        return
    rep.add("filler", len(text), 0)
    m["content"] = None
