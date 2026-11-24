"""Context strain: what the router had to do to fit a conversation into the model that served it, noted per request,
so a client can tell when a thread has outgrown what the free sources handle well (Provenance.app then suggests
continuing in Codex or Claude Code instead of letting the agent loop on a history it can no longer see whole).

Flags (one request can carry several):

- compacted   an API model's context window was smaller than the history: the old part went into a brief (compact.py)
- trimmed     even the latest cut compaction allows left too much: the kept turns' tool output was shortened as well
- summarised  a web chat's fresh first message carried a brief of the history instead of the history
- shortened   a web chat's fresh first message had the history cut down without a brief
- restarted   a web chat was full or stopped taking messages, and the request carried on in a fresh chat
- over        no model of the tier could take the history at all (the request failed with no capacity)

`compacted` and `summarised` are the steady state of any long thread and mean little alone; `trimmed`, `restarted`
and `over` (STRONG) mean the history no longer fits even after compaction.

How: a front end starts a notes dict per request (begin()); the router adds flags while it serves (note_*()). The
dict is shared by reference with every task the request starts, so a flag a streamed web reply adds after the
response began still lands before the front end writes its ledger entry at the end. The usage ledger stores the
flags on the request's entry ("ctx") and GET /v1/pcore/usage sums them per turn ("context")."""

from __future__ import annotations

from contextvars import ContextVar

FLAGS = ("compacted", "trimmed", "summarised", "shortened", "restarted", "over")
STRONG = ("trimmed", "restarted", "over")

_NOTES: ContextVar[dict | None] = ContextVar("pcore_context_notes", default=None)


def begin() -> dict:
    """A fresh notes dict for the request being served now (and the tasks it starts)."""
    notes: dict = {}
    _NOTES.set(notes)
    return notes


def note(*flags: str) -> None:
    notes = _NOTES.get()
    if notes is None:
        return  # not inside a request that asked (calibration, an ensemble member, a test calling the router)
    for f in flags:
        if f in FLAGS:
            notes[f] = True


def note_compaction(report: dict | None) -> None:
    """compact.py's report for an API request (None: nothing was compacted)."""
    if report:
        note("compacted", *(("trimmed",) if report.get("trimmed") else ()))


def note_web(web: dict | None) -> None:
    """The web bridge's record of one reply (data["pcore"]["web"])."""
    if not web:
        return
    if web.get("restarted"):
        note("restarted")
    comp = web.get("compaction")
    if comp:
        note("summarised", *(("trimmed",) if comp.get("trimmed") else ()))
    elif web.get("compacted"):
        note("shortened")


def flags(notes: dict | None) -> list[str]:
    return [f for f in FLAGS if (notes or {}).get(f)]
