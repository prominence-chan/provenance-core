"""How to get a free API key from each provider of the token pool (GET /pool/help).

The text is data, in pool_help.yaml beside this file: one entry per API provider, in the order that gives the most
free capacity for the least effort. The app shows it, and an assistant walking someone through the sign-ups is
handed it. `python -m provenance_core.pool_help [path]` writes docs/pool-keys.md from the same data (a test compares
the two, so they cannot drift)."""

from __future__ import annotations

import datetime
import sys
from functools import lru_cache
from pathlib import Path

import yaml

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
        '`PUT /pool/keys/{id}` with `{"values": {"<VARIABLE>": "<key>"}}`). The same data is served as JSON by '
        "`GET /pool/help`.",
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
        out += [
            "",
            f"Checked {p['checked']} ({p['confidence']}). Sources: " + ", ".join(f"<{u}>" for u in p["sources"]),
        ]
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DOC
    target.write_text(render(), encoding="utf-8")
    print(f"wrote {target}")
