"""Per-model and per-provider-pool usage windows (to stay under each free-tier cap) and health (circuit breaker + rate-limit cooldowns)."""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from .catalog import Model


@dataclass
class Usage:
    minute: deque = field(default_factory=deque)   # (t, tokens, share of a shared daily allowance)
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
    latency_ms: float | None = None   # EWMA
    ok: int = 0
    errors: int = 0


class State:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.usage: dict[str, Usage] = {}
        self.health: dict[str, Health] = {}
        self.sessions: dict[str, tuple[str, float]] = {}   # session -> (model id, last used)
        self.lock = threading.Lock()

    def _u(self, mid):
        return self.usage.setdefault(mid, Usage())

    def _h(self, mid):
        return self.health.setdefault(mid, Health())

    @staticmethod
    def _pool_key(m: Model) -> str:
        return f"@{m.provider}"   # usage shared by every model of the provider

    def _windows(self, key: str, now: float) -> tuple[int, int, int, int, int, float]:
        """(requests/min, requests/hour, requests/day, tokens/min, tokens/day, share of the daily allowance used)."""
        with self.lock:
            u = self._u(key)
            u.prune(now)
            minute, day = list(u.minute), list(u.day)
        return (len(minute), sum(1 for e in day if now - e[0] <= 3600), len(day), sum(e[1] for e in minute),
                sum(e[1] for e in day), sum(e[2] for e in day))

    def headroom(self, m: Model, tokens: int) -> float:
        """Fraction (0-1) of the tightest remaining limit after this request; 0 = would exceed a cap. Counts the
        model's own limits and its provider's pool."""
        now = self.clock()
        checks = []
        rm, rh, rd, tm, td, _ = self._windows(m.id, now)
        L = m.limits
        checks += [(rm, L.rpm, 1), (rh, L.rph, 1), (rd, L.rpd, 1), (tm, L.tpm, tokens), (td, L.tpd, tokens)]
        if m.pool or m.shared_tpd:
            rm, rh, rd, tm, td, share = self._windows(self._pool_key(m), now)
            if m.pool:
                P = m.pool
                checks += [(rm, P.rpm, 1), (rh, P.rph, 1), (rd, P.rpd, 1), (tm, P.tpm, tokens), (td, P.tpd, tokens)]
            if m.shared_tpd:
                checks.append((share, 1.0, tokens / m.limits.tpd))
        fracs = []
        for used, cap, add in checks:
            if cap:
                if used + add > cap:
                    return 0.0
                fracs.append(1 - (used + add) / cap)
        return max(min(fracs), 1e-3) if fracs else 1.0   # exactly reaching a cap is still allowed

    def available(self, m: Model) -> bool:
        return self._h(m.id).cooldown_until <= self.clock()

    def record(self, m: Model, tokens: int):
        now = self.clock()
        with self.lock:
            keys = [m.id] + ([self._pool_key(m)] if m.pool or m.shared_tpd else [])
            for k in keys:
                u = self._u(k)
                share = tokens / m.limits.tpd if k != m.id and m.shared_tpd else 0.0
                u.minute.append((now, tokens, share))
                u.day.append((now, tokens, share))

    def success(self, m: Model, latency_ms: float):
        h = self._h(m.id)
        h.failures, h.ok = 0, h.ok + 1
        h.latency_ms = latency_ms if h.latency_ms is None else 0.8 * h.latency_ms + 0.2 * latency_ms

    def failure(self, m: Model, retry_after: float | None = None, rate_limited: bool = False):
        """Rate limits cool down for Retry-After (or 60s); other errors back off exponentially (30s, 60s, ... 1h)."""
        h = self._h(m.id)
        h.errors += 1
        if rate_limited:
            h.cooldown_until = self.clock() + (retry_after if retry_after else 60)
            return
        h.failures += 1
        if h.failures >= 2:
            h.cooldown_until = self.clock() + min(3600, 30 * math.pow(2, h.failures - 2))

    def sticky(self, session: str | None) -> str | None:
        if not session:
            return None
        v = self.sessions.get(session)
        return v[0] if v and self.clock() - v[1] < 6 * 3600 else None

    def remember(self, session: str | None, mid: str):
        if session:
            self.sessions[session] = (mid, self.clock())
