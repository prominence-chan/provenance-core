"""How to get a free API key from each provider of the token pool (GET /pool/help), and what happened when people did.

The text is data, in pool_help.yaml beside this file: one entry per API provider, in the order that gives the most
free capacity for the least effort. The app shows it, and an assistant walking someone through the sign-ups is
handed it. `python -m provenance_core.pool_help [path]` writes docs/pool-keys.md from the same data (a test compares
the two, so they cannot drift).

Outcomes: each time someone goes through a provider's steps (with the app's assistant, or by hand), the app reports
how it went (POST /pool/help/{provider}/outcome) and a dated line is appended to <PCORE_HOME>/pool-help-outcomes.jsonl:
the result, the minutes it took, notes per step, the labels seen on the page. Never a key and nothing personal: a
report holding anything shaped like a key or an email address is refused whole. GET /pool/help gives each provider
its `experience` from that file, and `python -m provenance_core.pool_help --review` prints, per provider, the step
notes that disagree with the written steps, for a person (or an agent) to correct pool_help.yaml by. Nothing here
rewrites the instructions."""

from __future__ import annotations

import datetime
import json
import math
import os
import re
import sys
from collections import Counter
from functools import lru_cache
from pathlib import Path

import yaml

from .settings import home_dir

FILE = Path(__file__).with_name("pool_help.yaml")
DOC = Path(__file__).resolve().parents[1] / "docs" / "pool-keys.md"


def _plain(x):
    """YAML dates as ISO strings, so the data is the same as its JSON."""
    if isinstance(x, dict):
        return {k: _plain(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_plain(v) for v in x]
    return x.isoformat() if isinstance(x, datetime.date) else x


@lru_cache(maxsize=1)
def load() -> dict:
    return _plain(yaml.safe_load(FILE.read_text(encoding="utf-8")))


def provider(pid: str) -> dict:
    for p in load()["providers"]:
        if p["id"] == pid:
            return p
    raise KeyError(f"There are no key instructions for a provider called {pid}.")


# ---- may a person add a second account's key? ------------------------------------------------------------------------
MULTIPLE = {
    "yes": "nothing against it found",
    "check_terms": "read the terms first",
    "no": "the provider's terms forbid a second account",
}
_NO_TERMS = {
    "allowed": "check_terms",
    "note": "This provider's terms were not read; read them before adding a key from a second account.",
}


def multiple_keys(pid: str) -> dict:
    """{"allowed": "yes" | "check_terms" | "no", "note"} for a provider: what its own terms say about one person
    using several accounts. "no" does not stop a second key being saved (a separate paid account is legitimate);
    it is what the app tells the person first."""
    try:
        mk = provider(pid).get("multiple_keys")
    except KeyError:
        mk = None
    if isinstance(mk, dict) and mk.get("allowed") in MULTIPLE:
        return {"allowed": mk["allowed"], "note": str(mk.get("note") or "")}
    return dict(_NO_TERMS)


# ---- what happened when people followed the steps --------------------------------------------------------------------
RESULTS = ("saved_working", "saved_rejected", "skipped", "gave_up")
SOURCES = ("assistant", "manual")
_TOKEN = re.compile(r"[A-Za-z0-9_\-]{20,}")
_PREFIXED = re.compile(
    r"(?<![A-Za-z0-9])(sk-|sk_|gsk_|csk-|eyJ|AIza|hf_|cf[ua]t_|nvapi-|vck_|cpk_|xai-|ghp_|github_pat_|pk_)[A-Za-z0-9_\-]{12,}"
)
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[A-Za-z]{2,}")


class OutcomeError(ValueError):
    """A report that is not taken; `str(e)` says why, never quoting what was sent."""


def outcomes_file(home: Path | None = None) -> Path:
    return Path(home or home_dir()) / "pool-help-outcomes.jsonl"


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


@lru_cache(maxsize=1)
def _key_patterns() -> list:
    out = []
    for p in load()["providers"]:
        for holder in [p, *(p.get("extra_fields") or [])]:
            for k in ("key_pattern", "pattern"):
                if holder.get(k):
                    out.append(re.compile(holder[k]))
    return out


def _mixed(s: str) -> bool:
    return bool(re.search(r"\d", s) and re.search(r"[A-Za-z]", s))


def looks_like_key(text: str) -> bool:
    """Could `text` hold an API key (or an account id)? True for anything matching a provider's documented key
    shape, a known key prefix, or a long run of mixed letters and digits with the randomness of a secret. Errs
    towards yes: a refused note costs a retyped sentence, a stored key cannot be taken back."""
    if _PREFIXED.search(text):
        return True
    words = text.split()
    if any(p.match(w.strip("\"'`.,;:()[]<>")) for w in words for p in _key_patterns()):
        return True
    for t in _TOKEN.findall(text):
        # a model id ("nemotron-3-120b-a12b") is short runs joined by hyphens; a secret has a long unbroken run of
        # mixed letters and digits, or is long and random all the way through
        if any(len(seg) >= 20 and _mixed(seg) and _entropy(seg) >= 3.0 for seg in re.split(r"[-_]", t)):
            return True
        if len(t) >= 32 and _mixed(t) and _entropy(t) >= 3.8:
            return True
    return False


def _text(v, what: str, limit: int) -> str:
    if not isinstance(v, str) or not v.strip() or len(v) > limit:
        raise OutcomeError(f"{what} is text of up to {limit} characters.")
    v = " ".join(v.split())
    if looks_like_key(v):
        raise OutcomeError(f"{what} looks as if it holds a key or an account id; nothing was recorded. Leave it out.")
    if _EMAIL.search(v):
        raise OutcomeError(f"{what} holds an email address; nothing was recorded. Leave personal details out.")
    return v


def record_outcome(pid: str, body: dict, home: Path | None = None, now: datetime.datetime | None = None) -> dict:
    """Check a report and append it. Raises KeyError (no such provider) or OutcomeError (not taken)."""
    provider(pid)
    if not isinstance(body, dict):
        raise OutcomeError("Send the outcome as a JSON object.")
    extra = [k for k in body if k not in ("result", "minutes", "step_notes", "labels_seen", "source")]
    if extra:
        raise OutcomeError("Only result, minutes, step_notes, labels_seen and source are recorded.")
    if body.get("result") not in RESULTS:
        raise OutcomeError(f"result is one of {', '.join(RESULTS)}.")
    if body.get("source") not in SOURCES:
        raise OutcomeError(f"source is one of {', '.join(SOURCES)}.")
    now = now or datetime.datetime.now(datetime.UTC)
    row: dict = {
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date": now.date().isoformat(),
        "provider": pid,
        "result": body["result"],
        "source": body["source"],
    }
    mins = body.get("minutes")
    if mins is not None:
        if isinstance(mins, bool) or not isinstance(mins, (int, float)) or not 0 <= mins <= 600:
            raise OutcomeError("minutes is a number from 0 to 600.")
        row["minutes"] = round(float(mins), 1)
    notes = body.get("step_notes")
    if notes is not None:
        if not isinstance(notes, list) or len(notes) > 40:
            raise OutcomeError("step_notes is a list of up to 40 notes.")
        out = []
        for n in notes:
            if not isinstance(n, dict) or not isinstance(n.get("ok"), bool):
                raise OutcomeError('Each step note is {"step": <number or text>, "ok": true | false, "note": "..."}.')
            step = n.get("step")
            if isinstance(step, bool) or not isinstance(step, (int, str)):
                raise OutcomeError("A step is its number (1 for the first) or its text.")
            if isinstance(step, int) and not 0 <= step <= 100:
                raise OutcomeError("A step number is between 1 and the number of steps.")
            one = {"step": step if isinstance(step, int) else _text(step, "A step", 300), "ok": n["ok"]}
            if n.get("note") is not None:
                one["note"] = _text(n["note"], "A step note", 500)
            out.append(one)
        row["step_notes"] = out
    labels = body.get("labels_seen")
    if labels is not None:
        if not isinstance(labels, list) or len(labels) > 60:
            raise OutcomeError("labels_seen is a list of up to 60 labels.")
        row["labels_seen"] = [_text(x, "A label", 120) for x in labels]
    p = outcomes_file(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def outcomes(home: Path | None = None) -> list[dict]:
    try:
        lines = outcomes_file(home).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("provider") and r.get("result") in RESULTS:
            out.append(r)
    return out


def _step_no(entry: dict | None, step) -> int | str:
    """A reported step as its number in the written steps (1 = the first) where it can be told, else its text."""
    steps = (entry or {}).get("steps") or []
    if isinstance(step, int):
        return step
    for i, s in enumerate(steps, 1):
        if step.strip().lower() == s.strip().lower():
            return i
    return step


def _trouble(pid: str, rows: list[dict]) -> list[dict]:
    """The steps reported as not matching or not working, most often reported first, with the notes given."""
    try:
        entry = provider(pid)
    except KeyError:
        entry = None
    by: dict = {}
    for r in rows:
        for n in r.get("step_notes") or []:
            if isinstance(n, dict) and n.get("ok") is False:
                t = by.setdefault(_step_no(entry, n.get("step")), {"count": 0, "notes": []})
                t["count"] += 1
                if n.get("note") and n["note"] not in t["notes"]:
                    t["notes"].append(n["note"])
    out = [{"step": k, "count": v["count"], "notes": v["notes"]} for k, v in by.items()]
    return sorted(out, key=lambda t: (-t["count"], str(t["step"])))


def experience(pid: str, rows: list[dict] | None = None) -> dict:
    """{tries, worked, last_worked, common_trouble} for one provider, from the outcomes file."""
    rows = [r for r in (outcomes() if rows is None else rows) if r.get("provider") == pid]
    worked = [r for r in rows if r["result"] == "saved_working"]
    return {
        "tries": len(rows),
        "worked": len(worked),
        "last_worked": max((r.get("date") or "" for r in worked), default=None) or None,
        "common_trouble": [{**t, "notes": t["notes"][:3]} for t in _trouble(pid, rows)[:5]],
    }


def review(home: Path | None = None) -> str:
    """Per provider with reports: the step notes that disagree with the written steps, and the labels people saw
    that the steps never mention. For correcting pool_help.yaml by hand."""
    rows = outcomes(home)
    if not rows:
        return f"No outcomes recorded yet ({outcomes_file(home)}).\n"
    out = []
    ids = [p["id"] for p in load()["providers"]]
    for pid in ids + sorted({r["provider"] for r in rows} - set(ids)):
        mine = [r for r in rows if r["provider"] == pid]
        if not mine:
            continue
        try:
            entry = provider(pid)
        except KeyError:
            entry = {"steps": [], "name": pid}
        counts = Counter(r["result"] for r in mine)
        mins = [r["minutes"] for r in mine if r["result"] == "saved_working" and "minutes" in r]
        out.append(
            f"== {pid} ({entry.get('name', pid)}): {len(mine)} tries, "
            + ", ".join(f"{counts[k]} {k}" for k in RESULTS if counts[k])
        )
        if mins:
            out.append(
                f"   minutes when it worked: median {sorted(mins)[len(mins) // 2]:g} (written: {entry.get('time_minutes', '?')})"
            )
        trouble = _trouble(pid, mine)
        if not trouble:
            out.append("   no step was reported as wrong")
        for t in trouble:
            n = t["step"]
            if isinstance(n, int) and 1 <= n <= len(entry["steps"]):
                out.append(f"   step {n}, reported wrong {t['count']}x. Written: {entry['steps'][n - 1]}")
            else:
                out.append(f"   a step not among the written ones, reported wrong {t['count']}x: {n}")
            out += [f"      - {note}" for note in t["notes"]] or ["      - (no note given)"]
        text = " ".join([*entry["steps"], *(entry.get("gotchas") or [])]).lower()
        unseen = Counter(x for r in mine for x in r.get("labels_seen") or [] if x.lower() not in text)
        if unseen:
            out.append(
                "   labels seen on the page that the steps do not mention: "
                + ", ".join(f"{k} ({v}x)" for k, v in unseen.most_common(12))
            )
        out.append("")
    return "\n".join(out) + "\n"


FREE = {
    "anonymous": "works without a key",
    "free": "free tier",
    "trial": "one-time trial",
    "none": "no free tier",
}


def render(data: dict | None = None) -> str:
    d = data or load()
    out = [
        "# Free API keys for the token pool",
        "",
        "<!-- Generated from provenance_core/pool_help.yaml by `python -m provenance_core.pool_help`. Edit the YAML. -->",
        "",
        f"How to get a free key from each provider the pool can use, most capacity for least effort first. Read from "
        f"the providers' own public pages on {d['checked']}; free tiers and button labels change often, so where a "
        "page differs from this, the page is right. Paste each key into the app's field for that provider (or "
        '`PUT /pool/keys/{id}` with `{"values": {"<VARIABLE>": "<key>"}}`; a further key from another account of '
        'yours with `POST /pool/keys/{id}` `{"key": "<key>"}`). The same data is served as JSON by `GET /pool/help`.',
        "",
        "Confidence: **seen** = the provider's current docs name the controls; **docs-only** = the docs say where "
        "the key is made but the page is behind a sign-in, so its labels are not given; **uncertain** = the only "
        "walkthrough is dated or the docs disagree.",
        "",
        "| # | Provider | Free | Needs | Minutes | Confidence |",
        "|---|---|---|---|---|---|",
    ]
    for i, p in enumerate(d["providers"], 1):
        out.append(
            f"| {i} | [{p['name']}](#{p['id']}) | {FREE[p['free_tier']]} | {p['needs']} | {p['time_minutes']} "
            f"| {p['confidence']} |"
        )
    for i, p in enumerate(d["providers"], 1):
        out += ["", f'<a id="{p["id"]}"></a>', f"## {i}. {p['name']} (`{p['id']}`)", "", p["what_you_get"], ""]
        if p.get("until"):
            out += [f"**Temporary: used until {p['until']}.** After that day the router stops offering its models.", ""]
        out.append(f"- **Needs:** {p['needs']}")
        out.append(f"- **Sign up:** <{p['signup_url']}>")
        out.append(f"- **Key page:** <{p['keys_url']}>")
        if p.get("key_var"):
            out.append(f"- **Goes into:** `{p['key_var']}`")
        out.append(f"- **Key looks like:** {p['key_format']}")
        for f in p.get("extra_fields") or []:
            out.append(f"- **Also needed, {f['label']}** (`{f['var']}`): {f['where']} {f.get('format', '')}".rstrip())
        out += ["", "Steps:", ""]
        out += [f"{n}. {s}" for n, s in enumerate(p["steps"], 1)]
        out += ["", "Good to know:", ""]
        out += [f"- {g}" for g in p["gotchas"]]
        if p.get("terms_note"):
            out += ["", f"Terms: {p['terms_note']}"]
        mk = p.get("multiple_keys") if (p.get("multiple_keys") or {}).get("allowed") in MULTIPLE else _NO_TERMS
        out += ["", f"Keys from several accounts: {MULTIPLE[mk['allowed']]}. {mk['note']}".rstrip()]
        out += [
            "",
            f"Checked {p['checked']} ({p['confidence']}). Sources: " + ", ".join(f"<{u}>" for u in p["sources"]),
        ]
    if d.get("removed"):
        out += ["", "## Removed from the pool", "", "A key saved for one of these is ignored.", ""]
        out += ["| Provider | Removed | Why |", "|---|---|---|"]
        out += [f"| {r['name']} (`{r['id']}`) | {r['removed']} | {r['reason']} |" for r in d["removed"]]
    if d.get("considered"):
        out += [
            "",
            "## Looked at and not added",
            "",
            f"Other providers checked on {d['checked']} for a standing free API allowance that needs no card.",
            "",
            "| Provider | Free allowance today | Why not | Source |",
            "|---|---|---|---|",
        ]
        out += [f"| {c['name']} | {c['allowance']} | {c['why_not']} | <{c['source']}> |" for c in d["considered"]]
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--review":
        sys.stdout.write(review(Path(args[1]) if len(args) > 1 else None))
    else:
        target = Path(args[0]) if args else DOC
        target.write_text(render(), encoding="utf-8")
        print(f"wrote {target}")
