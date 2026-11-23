"""Per-(model, key) and per-provider-pool usage windows (to stay under each free-tier cap) and health (circuit breaker + rate-limit cooldowns)."""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field

from .catalog import Model


@dataclass
class Usage:
    minute: deque = field(default_factory=deque)  # (t, tokens, share of a shared daily allowance)
    day: deque = field(default_factory=deque)

    def prune(self, now: float):
        while self.minute and now - self.minute[0][0] > 60:
            self.minute.popleft()
        while self.day and now - self.day[0][0] > 86400:
            self.day.popleft()


@dataclass
class Health:
    failures: int = 0
    cooldown_until: float = 0.0
    latency_ms: float | None = None  # EWMA
    ok: int = 0
    errors: int = 0


SERVED: ContextVar[dict | None] = ContextVar("pcore_served_keys", default=None)  # this request: model id -> key id


class State:
    """Usage and rate-limit cooldowns are kept per (model, key): each key is another account with its own allowance.
    Latency and failures that are the model's own (a 500, a timeout) stay per model. A method called without a key
    speaks for the model as a whole: the best of its keys (headroom), any of them (available)."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.usage: dict[str, Usage] = {}
        # per model under its id; a key's rate-limit cooldown under "<model id>#<key id>" in the same dict, so
        # clearing it starts everything over
        self.health: dict[str, Health] = {}
        self.sessions: dict[str, tuple[str, float]] = {}  # session -> (model id, last used)
        self.lock = threading.Lock()

    def _u(self, mid):
        return self.usage.setdefault(mid, Usage())

    def _h(self, mid):
        return self.health.setdefault(mid, Health())

    # ---- keys ------------------------------------------------------------------------------------------------
    @staticmethod
    def keys(m: Model) -> list:
        """The keys (pool_keys.PoolKey, the anonymous slot included) requests for `m` can use; [None] for a model
        keys do not apply to (a web chat) or whose provider has none right now."""
        p = getattr(m, "prov", None)
        if p is None or p.type != "openai":
            return [None]
        return p.slots() or [None]

    def _key(self, m: Model, key):
        """A key given as a PoolKey or an id, as one of the model's keys (None when it is not one)."""
        if key is None or not isinstance(key, str):
            return key
        return next((k for k in self.keys(m) if k is not None and k.id == key), None)

    @staticmethod
    def _slot(m: Model, key) -> str:
        return m.id if key is None else f"{m.id}#{key.id}"

    @staticmethod
    def _pool_key(m: Model, key=None) -> str:
        """Usage shared by every model of the provider: per key (each account has its own allowance), or one for
        the provider where its pool counts per IP address."""
        per_ip = getattr(getattr(m, "prov", None), "pool_per", "key") == "ip"
        return f"@{m.provider}" if key is None or per_ip else f"@{m.provider}#{key.id}"

    @staticmethod
    def limits(m: Model, key=None):
        """(the model's limits, its provider's pool limits) as they apply to this key."""
        if key is None or not key.limits:
            return m.limits, m.pool
        from .pool_keys import limits_for

        return limits_for(key, m.limits, m.model, m.pool)

    def note_served(self, m: Model, key):
        """Remember, for the rest of this request, which key served `m` (so usage recorded later lands on it)."""
        if key is not None:
            SERVED.set({**(SERVED.get() or {}), m.id: key.id})

    def served(self, m: Model) -> str | None:
        """The id of the key that served `m` in this request, if one did."""
        return (SERVED.get() or {}).get(m.id)

    # ---- usage -----------------------------------------------------------------------------------------------
    def _windows(self, key: str, now: float) -> tuple[int, int, int, int, int, float]:
        """(requests/min, requests/hour, requests/day, tokens/min, tokens/day, share of the daily allowance used)."""
        with self.lock:
            u = self._u(key)
            u.prune(now)
            minute, day = list(u.minute), list(u.day)
        return (
            len(minute),
            sum(1 for e in day if now - e[0] <= 3600),
            len(day),
            sum(e[1] for e in minute),
            sum(e[1] for e in day),
            sum(e[2] for e in day),
        )

    def _headroom(self, m: Model, tokens: int, key) -> float:
        now = self.clock()
        checks = []
        L, P = self.limits(m, key)
        rm, rh, rd, tm, td, _ = self._windows(self._slot(m, key), now)
        checks += [(rm, L.rpm, 1), (rh, L.rph, 1), (rd, L.rpd, 1), (tm, L.tpm, tokens), (td, L.tpd, tokens)]
        shared = m.shared_tpd and bool(L.tpd)
        if P or shared:
            rm, rh, rd, tm, td, share = self._windows(self._pool_key(m, key), now)
            if P:
                checks += [(rm, P.rpm, 1), (rh, P.rph, 1), (rd, P.rpd, 1), (tm, P.tpm, tokens), (td, P.tpd, tokens)]
            if shared:
                checks.append((share, 1.0, tokens / L.tpd))
        fracs = []
        for used, cap, add in checks:
            if cap:
                if used + add > cap:
                    return 0.0
                fracs.append(1 - (used + add) / cap)
        return max(min(fracs), 1e-3) if fracs else 1.0  # exactly reaching a cap is still allowed

    def key_headrooms(self, m: Model, tokens: int) -> list[tuple]:
        """(key, headroom) for each of the model's keys that is not cooling down, most headroom first (at a tie a
        key before the anonymous slot)."""
        out = [(k, self._headroom(m, tokens, k)) for k in self.keys(m) if self._key_ready(m, k)]
        return sorted(out, key=lambda x: (-x[1], x[0] is not None and x[0].value is None))

    def headroom(self, m: Model, tokens: int, key=None) -> float:
        """Fraction (0-1) of the tightest remaining limit after this request; 0 = would exceed a cap. Counts the
        model's own limits and its provider's pool. With a key: that key's. Without: the best of the model's keys
        (the one a request would be sent with)."""
        if key is not None:
            k = self._key(m, key)
            return self._headroom(m, tokens, k) if k is not None else 0.0
        return max((h for _, h in self.key_headrooms(m, tokens)), default=0.0)

    def capacity(self, m: Model, tokens: int) -> float:
        """The model's headroom added up over its keys: 1.0 for each key with its whole allowance left."""
        return sum(h for _, h in self.key_headrooms(m, tokens))

    def _key_ready(self, m: Model, key) -> bool:
        h = self.health.get(self._slot(m, key)) if key is not None else None
        return h is None or h.cooldown_until <= self.clock()

    def available(self, m: Model, key=None) -> bool:
        """Not cooling down: the model itself, and the key (without one: any of the model's keys)."""
        if self._h(m.id).cooldown_until > self.clock():
            return False
        if key is not None:
            k = self._key(m, key)
            return k is not None and self._key_ready(m, k)
        return any(self._key_ready(m, k) for k in self.keys(m))

    def _used_key(self, m: Model, key):
        """The key usage belongs to: the one named, else the one that served this request, else the model's first."""
        k = self._key(m, key if key is not None else self.served(m))
        if k is None:
            ks = self.keys(m)
            k = ks[0] if ks else None
        return k

    def record(self, m: Model, tokens: int, key=None):
        now = self.clock()
        k = self._used_key(m, key)
        L, P = self.limits(m, k)
        shared = m.shared_tpd and bool(L.tpd)
        with self.lock:
            keys = [self._slot(m, k)] + ([self._pool_key(m, k)] if P or shared else [])
            for i, name in enumerate(keys):
                u = self._u(name)
                share = tokens / L.tpd if i and shared else 0.0
                u.minute.append((now, tokens, share))
                u.day.append((now, tokens, share))

    def success(self, m: Model, latency_ms: float, key=None):
        h = self._h(m.id)
        h.failures, h.ok = 0, h.ok + 1
        h.latency_ms = latency_ms if h.latency_ms is None else 0.8 * h.latency_ms + 0.2 * latency_ms

    def failure(self, m: Model, retry_after: float | None = None, rate_limited: bool = False, key=None):
        """Rate limits cool down for Retry-After (or 60s): the key that was limited when one is named (the model's
        other keys go on), else the model. Other errors are the model's and back off exponentially (30s, ... 1h)."""
        h = self._h(m.id)
        h.errors += 1
        if rate_limited:
            until = self.clock() + (retry_after if retry_after else 60)
            k = self._key(m, key)
            if k is not None:
                self.health.setdefault(self._slot(m, k), Health()).cooldown_until = until
            else:
                h.cooldown_until = until
            return
        h.failures += 1
        if h.failures >= 2:
            h.cooldown_until = self.clock() + min(3600, 30 * math.pow(2, h.failures - 2))

    def forget(self, m: Model):
        """Start over for a model: its cooldowns (its own and its keys') are dropped."""
        for s in [s for s in self.health if s == m.id or s.startswith(m.id + "#")]:
            self.health.pop(s, None)

    def sticky(self, session: str | None) -> str | None:
        if not session:
            return None
        v = self.sessions.get(session)
        return v[0] if v and self.clock() - v[1] < 6 * 3600 else None

    def remember(self, session: str | None, mid: str):
        if session:
            self.sessions[session] = (mid, self.clock())
