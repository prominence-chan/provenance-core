"""Keeping a profile's saved sign-ins: backups of its cookie store, noticing a wiped store, putting a backup back
once, and not starting a browser on a wrong clock or a full disk.

Why (2026-11-23): after an ordinary restart all three profiles came up with an empty cookie store and nine site
sign-ins had to be redone by hand. Reproduced afterwards on a scratch profile with the same switches: Chrome for
Testing 158 deletes every cookie that 157 (or 153) wrote (157 stores values marked "v10", 158 "v11"; the rows are
deleted on load, the file keeps its size), and the same file opened by 157 again is intact. The installed Chrome had
updated itself to 158 a few minutes before, so the restart fetched 158 and opened the 157 profiles with it. What
this module does about it, whatever the cause of the next loss:

- backups (<home>/cookie-backups/<profile>/<stamp>-<kind>/, folders 0700, files 0600): a plain copy of the cookie
  database and its -journal / -wal once the browser has exited and nothing holds the profile, and before a launch
  when the file changed since; about hourly while a browser runs, through SQLite's backup API on a read-only
  connection that gives up at once when the file is busy. A copy is kept only if it opens, has a `cookies` table
  with rows, and has not collapsed against the last kept one while losing the session cookies of a signed-in site:
  an emptied store never rotates a good one out. The last few and one per day for a week are kept;
- detection: the row count and the session-cookie names per site (names and hosts only; the value columns are
  never selected) before a launch, a few seconds after it, again later (Chrome writes its deletions within 30 s)
  and when a page says "signed out". A site the bridge knows as signed in with none of its session cookies left,
  in a store whose row count collapsed, is a wiped store, not a sign-out;
- restore, once per profile per day: with the browser closed the wiped files are moved aside (kept) and the newest
  good backup is put in place. A backup written by another major version of the browser is only put back together
  with that version (browser_host.pin): the newer one would delete it again. A second wipe is reported, not
  repaired;
- the launch gate: no browser is started while the clock looks wrong or the disk is nearly full;
- one line in <home>/web-events.jsonl per launch, exit, backup, detection and restore, with the store's size, row
  count and per-site session-cookie counts, both clocks and the free disk space.

Cookie values never enter Python: files are copied as opaque bytes, SQLite copies pages itself, and every query
names its columns (host_key, name, dates)."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

from ..settings import env

SUFFIXES = ("", "-journal", "-wal")
DB_PATHS = ("Default/Network/Cookies", "Default/Cookies")
STARTED = time.monotonic()
RESTORING = "cookie-restore"  # Bridge.signin while a backup is put back: requests skip the account, no penalty
ASIDE_KEEP = 5

LOST_RESTORED = "This computer lost the browser's saved sign-ins; a backup was put back."
LOST_NO_BACKUP = "This computer lost the browser's saved sign-ins and no backup could be restored: sign in again."
LOST_AGAIN = (
    "This computer lost the browser's saved sign-ins again after a backup was put back; nothing more was changed: "
    "sign in again."
)
LOST_VERSION = (
    "The browser was updated and the new version cannot read the saved sign-ins; the previous version could not be "
    "brought back: sign in again."
)


def mono() -> float:
    """Seconds on a clock that keeps counting while the Mac sleeps (macOS CLOCK_MONOTONIC; time.monotonic() stops
    there), so a night asleep does not look like a clock jump."""
    try:
        return time.clock_gettime(time.CLOCK_MONOTONIC)
    except (AttributeError, OSError):
        return time.monotonic()


def free_bytes(path) -> int:
    return shutil.disk_usage(path).free


# ---- reading the store: names, hosts and dates only -------------------------------------------------------------------
def cookie_db(profile) -> Path | None:
    for rel in DB_PATHS:
        p = Path(profile) / rel
        if p.exists():
            return p
    return None


def _matches(name: str, names) -> bool:
    return any(name.startswith(n[:-1]) if n.endswith("*") else name == n for n in names)


def _in_domains(host, domains) -> bool:
    h = str(host).lstrip(".").lower()
    return not domains or any(h == d or h.endswith("." + d) for d in domains)


def _ro(db: Path, timeout: float = 1.0) -> sqlite3.Connection:
    return sqlite3.connect(Path(db).as_uri() + "?mode=ro", uri=True, timeout=timeout)


def session_cookies(profile: Path, spec: dict | None) -> dict:
    """{(host, name): (created, updated, expires)} of the site's session cookies in the profile. Only names, hosts
    and dates are read; the value columns are never selected. Chrome keeps the database open but does not lock out
    readers (measured on Chrome 157); a read that collides with a write is simply tried again on the next poll."""
    if not spec or not spec.get("names"):
        return {}
    db = cookie_db(profile)
    if not db:
        return {}
    domains = [d.lstrip(".").lower() for d in spec.get("domains") or []]
    try:
        con = _ro(db)
        try:
            cols = {r[1] for r in con.execute("pragma table_info(cookies)")}
            upd = "last_update_utc" if "last_update_utc" in cols else "0"
            rows = con.execute(f"select host_key, name, creation_utc, {upd}, expires_utc from cookies").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    return {
        (host, name): (created, updated, expires)
        for host, name, created, updated, expires in rows
        if _in_domains(host, domains) and _matches(str(name), spec["names"])
    }


def read_db(db, specs: dict, check: bool = False) -> dict:
    """{"size", "rows", "sites": {site: its session cookies}} of one cookie database file. rows is None when the
    file cannot be read (busy, damaged, no `cookies` table). `check` also runs SQLite's quick integrity check."""
    out: dict = {"size": None, "rows": None, "sites": {}}
    try:
        out["size"] = sum(Path(str(db) + s).stat().st_size for s in SUFFIXES if Path(str(db) + s).exists())
        con = _ro(Path(db))
        try:
            if check and con.execute("pragma quick_check(1)").fetchone()[0] != "ok":
                return out
            rows = con.execute("select host_key, name from cookies").fetchall()
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return out
    out["rows"] = len(rows)
    for site, spec in specs.items():
        domains = [d.lstrip(".").lower() for d in (spec or {}).get("domains") or []]
        names = (spec or {}).get("names") or []
        out["sites"][site] = sum(1 for h, n in rows if _in_domains(h, domains) and _matches(str(n), names))
    return out


def snapshot(profile, specs: dict) -> dict:
    """read_db of the profile's cookie store; a profile without one counts as no rows."""
    db = cookie_db(profile)
    if not db:
        return {"size": 0, "rows": 0, "sites": dict.fromkeys(specs, 0), "missing": True}
    return read_db(db, specs)


def collapsed(now: int, before: int, ratio: float = 0.25, floor: int = 10) -> bool:
    """From dozens or hundreds of rows to a handful (under `ratio` of what there was), or to none at all."""
    if not before or before <= 0:
        return False
    return now == 0 or (before >= floor and now < before * ratio)


def lost_sites(now: dict, base: dict | None, expect, ratio: float = 0.25, floor: int = 10) -> list[str]:
    """The sites in `expect` (known as signed in) that had session cookies in `base` and have none in `now`, when
    the row count has collapsed as well; [] when that is not what happened or it cannot be told."""
    if not base or now.get("rows") is None or base.get("rows") is None:
        return []
    if not collapsed(now["rows"], base["rows"], ratio, floor):
        return []
    was, has = base.get("sites") or {}, now.get("sites") or {}
    return sorted(s for s in expect if was.get(s, 0) > 0 and has.get(s, 0) == 0)


# ---- backups --------------------------------------------------------------------------------------------------------
def _private_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(p, 0o700)
    return p


def backup_root(home, profile) -> Path:
    top = _private_dir(Path(home) / "cookie-backups")
    return _private_dir(top / Path(profile).name)


def _write_private(path: Path, data: dict):
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _read_json(path: Path) -> dict:
    try:
        d = json.loads(Path(path).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def generations(home, profile) -> list[dict]:
    """The kept backups of a profile, newest first: each one's meta.json plus "dir"."""
    root = Path(home) / "cookie-backups" / Path(profile).name
    out = []
    try:
        dirs = [d for d in root.iterdir() if d.is_dir() and not d.name.startswith((".", "wiped-"))]
    except OSError:
        return []
    for d in dirs:
        m = _read_json(d / "meta.json")
        if isinstance(m.get("ts"), int | float) and (d / "Cookies").exists():
            out.append({**m, "dir": str(d)})
    return sorted(out, key=lambda m: (m["ts"], m["dir"]), reverse=True)


def _stamp(ts: float) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))


def _free_name(root: Path, name: str) -> Path:
    p, n = root / name, 1
    while p.exists():
        n += 1
        p = root / f"{name}-{n}"
    return p


def take(home, profile, specs: dict, kind: str, *, running: bool = False, expect=(), browser: str | None = None,
         wall: float | None = None, keep: int = 5, days: int = 7, ratio: float = 0.25, floor: int = 10) -> dict:  # fmt: skip
    """One backup of the profile's cookie store -> {"kept": bool, "why": None | "none" | "unchanged" | "busy" |
    "unreadable" | "empty" | "collapsed", "rows", "sites", "name"}. `running`: a browser has the file open, so it
    is copied through SQLite's backup API on a read-only connection, given up at once when the file is busy;
    otherwise (nothing holds the profile) the files are copied as they are. `expect`: the sites known as signed in."""
    ts = time.time() if wall is None else wall
    db = cookie_db(profile)
    if not db:
        return {"kept": False, "why": "none"}
    root = backup_root(home, profile)
    for stale in root.glob(".tmp-*"):  # a copy that was interrupted
        shutil.rmtree(stale, ignore_errors=True)
    gens = generations(home, profile)
    prev = gens[0] if gens else None
    st = db.stat()
    if prev and (prev.get("src_mtime_ns"), prev.get("src_size")) == (st.st_mtime_ns, st.st_size):
        return {"kept": False, "why": "unchanged"}
    tmp = _private_dir(root / f".tmp-{os.getpid()}-{time.monotonic_ns()}")
    try:
        if running:
            if not _sqlite_copy(db, tmp / "Cookies"):
                return {"kept": False, "why": "busy"}
        else:
            for s in SUFFIXES:
                if Path(str(db) + s).exists():
                    shutil.copyfile(str(db) + s, tmp / ("Cookies" + s))
        for f in tmp.iterdir():
            os.chmod(f, 0o600)
        got = read_db(tmp / "Cookies", specs, check=True)
        out = {"rows": got["rows"], "sites": got["sites"]}
        if got["rows"] is None:
            return {"kept": False, "why": "unreadable", **out}
        if got["rows"] == 0:
            return {"kept": False, "why": "empty", **out}
        # judged against the last kept one, unless that is older than what is kept at all (a new start)
        if prev and ts - prev["ts"] < days * 86400 and lost_sites(got, prev, expect, ratio, floor):
            return {"kept": False, "why": "collapsed", **out}
        meta = {"ts": round(ts, 3), "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)), "kind": kind,
                "rows": got["rows"], "sites": got["sites"], "size": got["size"], "browser": browser,
                "rel": str(db.relative_to(profile)), "src_mtime_ns": st.st_mtime_ns, "src_size": st.st_size,
                "files": sorted(f.name for f in tmp.iterdir())}  # fmt: skip
        _write_private(tmp / "meta.json", meta)
        final = _free_name(root, f"{_stamp(ts)}-{kind}")
        tmp.rename(final)
        rotate(home, profile, keep, days, ts)
        return {"kept": True, "why": None, "name": final.name, **out}
    except (OSError, sqlite3.Error):
        return {"kept": False, "why": "unreadable"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _sqlite_copy(db: Path, dest: Path, budget_s: float = 2.0) -> bool:
    """A consistent copy of a database a browser has open. Read-only on the source; when the file is locked (the
    browser is writing) or the copy takes long, it is dropped instead of waited for: the browser is never held up."""
    end = time.monotonic() + budget_s

    def progress(status, remaining, total):
        if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or time.monotonic() > end:
            raise TimeoutError

    try:
        src = _ro(db, timeout=0.2)
        try:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst, progress=progress, sleep=0.05)
            finally:
                dst.close()
        finally:
            src.close()
        return True
    except (sqlite3.Error, TimeoutError, OSError):
        return False


def rotate(home, profile, keep: int = 5, days: int = 7, wall: float | None = None):
    """Keep the newest `keep` backups and the newest of each of the last `days` local days; remove the others."""
    now = time.time() if wall is None else wall
    gens = generations(home, profile)
    stay = {g["dir"] for g in gens[: max(1, keep)]}
    seen_days = set()
    for g in gens:
        day = time.strftime("%Y-%m-%d", time.localtime(g["ts"]))
        if now - g["ts"] <= days * 86400 and day not in seen_days:
            seen_days.add(day)
            stay.add(g["dir"])
    for g in gens:
        if g["dir"] not in stay:
            shutil.rmtree(g["dir"], ignore_errors=True)
    root = Path(home) / "cookie-backups" / Path(profile).name
    for old in sorted(root.glob("wiped-*"), reverse=True)[ASIDE_KEEP:]:
        shutil.rmtree(old, ignore_errors=True)


def restore(home, profile, gen: dict, wall: float | None = None) -> dict:
    """Put the backup `gen` in place of the profile's cookie store. Only with no browser on the profile. What was
    there is moved to <backups>/wiped-<stamp>/ and kept. -> {"aside": its folder's name}."""
    ts = time.time() if wall is None else wall
    profile = Path(profile)
    root = backup_root(home, profile)
    db = cookie_db(profile) or profile / (gen.get("rel") or DB_PATHS[1])
    aside = _private_dir(_free_name(root, f"wiped-{_stamp(ts)}"))
    for s in SUFFIXES:
        if Path(str(db) + s).exists():
            shutil.move(str(db) + s, aside / ("Cookies" + s))
    db.parent.mkdir(parents=True, exist_ok=True)
    for s in SUFFIXES:
        src = Path(gen["dir"]) / ("Cookies" + s)
        if src.exists():
            tmp = Path(str(db) + s + ".restoring")
            shutil.copyfile(src, tmp)
            os.chmod(tmp, 0o600)
            tmp.replace(str(db) + s)
    return {"aside": aside.name}


# ---- the clock ----------------------------------------------------------------------------------------------------
def _dur(s: float) -> str:
    s = abs(s)
    if s >= 2 * 86400:
        return f"{s / 86400:.0f} days"
    if s >= 2 * 3600:
        return f"{s / 3600:.0f} hours"
    return f"{s / 60:.0f} minutes" if s >= 120 else f"{s:.0f} seconds"


class Clock:
    """Is the wall clock believable? It is compared with a clock that only counts (since this object was made,
    and since the last note left in <home> by an earlier process of the same boot) and with the newest timestamps
    the router has written. A clock that then stays consistent for `settle_s` is taken as the time after all: a
    clock that was wrong for a moment corrects itself, one that stays put is right and what was written is not."""

    def __init__(self, wall=time.time, mono=mono):
        self.wall, self.mono = wall, mono
        self.a_wall, self.a_mono = wall(), mono()
        self.suspect: float | None = None  # on the counting clock: since when the wall clock is doubted
        self.accepted: float | None = None  # the written timestamp the clock was accepted against
        self.sticky: tuple[float, str] | None = None

    def check(self, refs=(), saved: dict | None = None, forward_s: float = 86400.0, back_s: float = 120.0,
              settle_s: float = 600.0) -> str | None:  # fmt: skip
        """None when the clock can be believed, else what is wrong with it in a few words."""
        now, m = self.wall(), self.mono()
        why = None
        drift = (now - self.a_wall) - (m - self.a_mono)
        if drift > forward_s or drift < -back_s:
            why = f"the clock jumped {'ahead' if drift > 0 else 'back'} by {_dur(drift)} while the router was running"
            self.a_wall, self.a_mono = now, m  # judged from here on
            self.sticky, self.suspect, self.accepted = (m + settle_s, why), m, None
        elif self.sticky and m < self.sticky[0]:
            why = self.sticky[1]
        else:
            confirmed = False
            if saved and isinstance(saved.get("wall"), int | float) and m >= (saved.get("mono") or float("inf")):
                d = (now - saved["wall"]) - (m - saved["mono"])
                if d > forward_s or d < -back_s:
                    why = f"the clock is {_dur(d)} {'ahead of' if d > 0 else 'behind'} where it was when last noted"
                else:
                    confirmed = True
            ref = max(refs, default=None)
            if not why and ref is not None and ref != self.accepted and not confirmed:
                if now < ref - back_s:
                    why = f"the clock is {_dur(ref - now)} behind the last thing the router wrote"
                elif now > ref + forward_s and self.a_wall > ref + back_s:  # this process did not see the gap pass
                    why = f"the clock is {_dur(now - ref)} ahead of the last thing the router wrote"
        if not why:
            self.suspect = None
            return None
        if self.suspect is None:
            self.suspect = m
        if m - self.suspect >= settle_s:
            self.suspect, self.sticky, self.accepted = None, None, max(refs, default=None)
            return None
        return why


def written_times(home) -> list[float]:
    """The newest timestamps the router itself has written under `home`: each account state's `checked_at`, the
    usage ledger's last line, the newest cookie backup of each profile."""
    home, out = Path(home), []
    for p in home.glob("*_web*.json"):
        v = _read_json(p).get("checked_at")
        if isinstance(v, int | float) and v > 0:
            out.append(float(v))
    ledger = Path(env("USAGE_LEDGER", str(home / "usage-ledger.jsonl")) or "")
    try:
        with open(ledger, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 8192))
            lines = [x for x in f.read().split(b"\n") if x.strip()]
        v = json.loads(lines[-1]).get("ts") if lines else None
        if isinstance(v, int | float) and v > 0:
            out.append(float(v))
    except (OSError, ValueError, AttributeError, IsADirectoryError):
        pass
    for d in (home / "cookie-backups").glob("*"):
        gens = generations(home, d.name) if d.is_dir() else []
        if gens:
            out.append(float(gens[0]["ts"]))
    return out


# ---- one profile's guard ---------------------------------------------------------------------------------------------
class Guard:
    """What the bridge that owns a profile's browser asks: may a browser start, what is in the store, take a
    backup, was it wiped, put a backup back. `wall`, `mono` and `free` can be replaced (tests)."""

    def __init__(self, bridge, wall=None, mono_clock=mono, free=free_bytes):
        self.b = bridge
        self.clock = Clock(wall or bridge.clock, mono_clock)
        self.free = free
        self.pre: dict | None = None  # the store just before the last launch
        self.restored: dict | None = None  # the backup put back before this launch, until it is checked
        self.pending: list[str] | None = None  # the sites a wipe was just seen for, until it is dealt with
        self.was: str | None = None  # the browser version that had last written the profile before the launch
        self._noted = 0.0

    @property
    def on(self) -> bool:
        return bool(self.b.o.get("cookie_guard", True)) and not self.b.host

    @property
    def home(self) -> Path:
        return self.b.home

    @property
    def profile(self) -> Path:
        return self.b.profile

    def bridges(self) -> list:
        return [self.b, *getattr(self.b, "_guests", [])]

    def specs(self) -> dict:
        return {x.display_name: x.session_spec for x in self.bridges() if x.session_spec}

    def expected(self) -> set[str]:
        """The sites the bridges on this profile know as signed in."""
        return {x.display_name for x in self.bridges() if x.session_spec and x.load().get("signed_in") is True}

    def snap(self) -> dict:
        return snapshot(self.profile, self.specs())

    def newest(self) -> dict | None:
        gens = generations(self.home, self.profile)
        return gens[0] if gens else None

    def _limits(self) -> dict:
        o = self.b.o
        return {"ratio": float(o["cookie_collapse_ratio"]), "floor": int(o["cookie_collapse_floor"])}

    def lost(self, now: dict, *bases) -> list[str]:
        """The signed-in sites whose session cookies are gone from a collapsed store, against any of `bases`."""
        exp = self.expected()
        for base in bases:
            got = lost_sites(now, base, exp, **self._limits())
            if got:
                return got
        return []

    def worth_watching(self, *bases) -> bool:
        exp = self.expected()
        return any(base and any((base.get("sites") or {}).get(s, 0) > 0 for s in exp) for base in bases)

    # -- events
    def event(self, kind: str, **more):
        """One line in <home>/web-events.jsonl: when, how long the router has been up, both clocks, free disk."""
        try:
            now = self.clock.wall()
            try:
                free_mb = int(self.free(self.home) // 1_000_000)
            except OSError:
                free_mb = None
            line = {
                "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                "kind": kind,
                "account": self.b.account or "main",
                "profile": self.profile.name,
                "up_s": int(time.monotonic() - STARTED),
                "wall": round(now, 1),
                "mono": round(self.clock.mono(), 1),
                "free_mb": free_mb,
                **more,
            }
            self.home.mkdir(parents=True, exist_ok=True)
            with open(self.home / "web-events.jsonl", "a") as f:
                f.write(json.dumps(line) + "\n")
        except (OSError, ValueError, OverflowError):
            pass

    @staticmethod
    def brief(snap: dict | None) -> dict | None:
        return None if snap is None else {k: snap.get(k) for k in ("size", "rows", "sites")}

    # -- the launch gate
    def gate(self) -> tuple[str, str] | None:
        """None when a browser may start, else (kind, reason): "disk" or "clock"."""
        o = self.b.o
        need = float(o["disk_min_free_mb"]) * 1_000_000
        try:
            free = self.free(self.home)
        except OSError:
            free = None
        if free is not None and need > 0 and free < need:
            return "disk", (
                f"only {int(free // 1_000_000)} MB are free on this disk; the browser is not started below "
                f"{int(o['disk_min_free_mb'])} MB (it could lose the saved sign-ins)"
            )
        note = self.home / "cookie-backups" / "clock.json"
        why = self.clock.check(
            written_times(self.home), _read_json(note), float(o["clock_forward_max_s"]),
            float(o["clock_back_slack_s"]), float(o["clock_settle_s"]),
        )  # fmt: skip
        if why:
            return "clock", f"{why}; the browser is not started on a clock that looks wrong"
        m = self.clock.mono()
        if m - self._noted > 60 or m < self._noted:
            self._noted = m
            try:
                _private_dir(note.parent)
                _write_private(note, {"wall": round(self.clock.wall(), 3), "mono": round(m, 3)})
            except OSError:
                pass
        return None

    # -- backups
    def backup(self, kind: str, running: bool = False, browser: str | None = None) -> dict:
        o = self.b.o
        r = take(self.home, self.profile, self.specs(), kind, running=running, expect=self.expected(),
                 browser=browser, wall=self.clock.wall(), keep=int(o["cookie_backup_keep"]),
                 days=int(o["cookie_backup_days"]), **self._limits())  # fmt: skip
        if r["why"] not in ("unchanged", "none"):
            self.event("cookie_backup", how=kind, **{k: r.get(k) for k in ("kept", "why", "rows", "sites", "name")})
        return r

    # -- restores: one per profile per local day
    def _book(self) -> Path:
        return backup_root(self.home, self.profile) / "guard.json"

    def _day(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(self.clock.wall()))

    def restored_today(self) -> bool:
        return any(r.get("day") == self._day() for r in _read_json(self._book()).get("restores") or [])

    def put_back(self, gen: dict) -> dict:
        r = restore(self.home, self.profile, gen, self.clock.wall())
        book = _read_json(self._book())
        book["restores"] = [*(book.get("restores") or [])[-19:],
                            {"day": self._day(), "ts": round(self.clock.wall(), 1), "backup": Path(gen["dir"]).name,
                             "browser": gen.get("browser"), "aside": r["aside"]}]  # fmt: skip
        _write_private(self._book(), book)
        return r
