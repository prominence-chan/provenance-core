"""The bridge's own browser (`browser: bundled` on a chatgpt_web provider): a Chrome for Testing build kept under
<PCORE_HOME>/browser (~/.tierlm/browser), run as a background-only app, instead of the user's installed Google Chrome.

Why a separate copy: the installed Chrome is the user's everyday app. Driving it on a second profile puts a second
"Google Chrome" in the Dock and app switcher, its windows can come forward, and it takes focus. A copy whose
Info.plist says LSBackgroundOnly has no Dock icon and can never become the active app; with no startup window and
its window created already off screen, nothing ever shows (macOS keeps a 40x41 px corner of an off-screen window).
Measured on macOS 26 (docs/TECHNICAL.md, "Bundled browser"):
- LSUIElement makes Chrome fail at launch ("icudtl.dat not found"); LSBackgroundOnly works
- an edited Info.plist breaks the bundle's signature and the kernel kills the process (exit 137): the copy is
  re-signed ad hoc. The copy is an APFS clone of the original (cp -c), so it costs no disk and is near instant
- a background-only app cannot take the keyboard: `weblogin` keeps using a normal window

Where the build comes from, in order: the app (or folder of apps) named by PCORE_BROWSER_APP, which is how
Provenance.app, run from anywhere, points at its own Contents/Resources/browser; an app shipped inside a
Provenance.app in /Applications or ~/Applications; one already prepared here; Playwright's cache; a download from Google's Chrome for
Testing bucket (~190 MB zip, ~370 MB unpacked). It must be at least as new as the installed Chrome's major version
and as the version that last wrote the profile ("Last Version"): an older browser must not open a newer profile.
Among the builds that qualify, the one closest to that major version is chosen, so that `weblogin` (the installed
Chrome) can still open the profile afterwards.

Nothing here touches a profile except reading its "Last Version" file."""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from .settings import env

CHROME_APP = Path("/Applications/Google Chrome.app")
CFT_INDEX = "https://googlechromelabs.github.io/chrome-for-testing/latest-versions-per-milestone-with-downloads.json"
CFT_CHANNELS = "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions.json"
SHIPPED = [  # where Provenance.app would carry its own copy (see docs/TECHNICAL.md, "Bundled browser")
    Path("/Applications/Provenance.app/Contents/Resources/browser"),
    Path.home() / "Applications/Provenance.app/Contents/Resources/browser",
]
PLAYWRIGHT_CACHE = Path.home() / "Library/Caches/ms-playwright"

# ---- launch switches ------------------------------------------------------------------------------------------------
# What the drivers pass to Chrome today for the bridge's launch_persistent_context (recorded from patchright 1.64 /
# playwright 1.65 with a stand-in executable). The bundled launch starts Chrome itself, so it repeats them; what is
# left out: the pipe and about:blank (the bundled browser is reached over a local debugging port and starts with no
# window), and --no-sandbox (Playwright's default; the renderer sandbox stays on for a browser that is signed in).
_FEATURES_PATCHRIGHT = (
    "AvoidUnnecessaryBeforeUnloadCheckSync,DestroyProfileOnBrowserClose,DialMediaRouteProvider,GlobalMediaControls,"
    "HttpsUpgrades,LensOverlay,PaintHolding,ThirdPartyStoragePartitioning,BlockOriginHeaderModificationOnRedirect,"
    "AvoidCorsURLLoaderRestartOnRedirect,Translate,AutoDeElevate,OptimizationHints,NetworkTimeServiceQuerying,"
    "AimEnabled"
).split(",")
_FEATURES_PLAYWRIGHT = (
    "AvoidUnnecessaryBeforeUnloadCheckSync,DestroyProfileOnBrowserClose,HttpsUpgrades,HttpsFirstBalancedModeAutoEnable,"
    "LensOverlay,MediaRouter,PaintHolding,ThirdPartyStoragePartitioning,AvoidCorsURLLoaderRestartOnRedirect,"
    "OptimizationHints,NetworkTimeServiceQuerying,AimEnabled,PreconnectToSearch"
).split(",")
_COMMON = [
    "--disable-field-trial-config",
    "--disable-background-networking",
    "--disable-breakpad",
    "--no-default-browser-check",
    "--disable-hang-monitor",
    "--disable-prompt-on-repost",
    "--disable-updater-scheduler",
    "--force-color-profile=srgb",
    "--no-first-run",
    "--password-store=basic",  # the same key storage as Playwright's and weblogin's launches: one cookie jar
    "--use-mock-keychain",
    "--no-service-autorun",
    "--disable-search-engine-choice-screen",
    "--disable-infobars",  # also hides Chrome for Testing's "not for browsing" bar
    "--disable-sync",
    # a window nobody sees must keep rendering and running timers, or replies stop arriving (the bridge's quiet trio)
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
]
DRIVER_ARGS = {
    "patchright": _COMMON + ["--export-tagged-pdf", "--disable-blink-features=AutomationControlled"],
    "playwright": _COMMON
    + [
        "--disable-back-forward-cache",
        "--disable-component-extensions-with-background-pages",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-extensions",
        "--allow-pre-commit-input",
        "--disable-ipc-flooding-protection",
        "--disable-popup-blocking",
        "--metrics-recording-only",
        "--unsafely-disable-devtools-self-xss-warnings",
        "--enable-unsafe-swiftshader",
        "--disable-stack-profiler",
    ],
}
DRIVER_FEATURES = {"patchright": _FEATURES_PATCHRIGHT, "playwright": _FEATURES_PLAYWRIGHT}
ENABLE_FEATURES = {"patchright": ["CDPScreenshotNewSurface"], "playwright": ["CDPScreenshotNewSurface"]}

# The trimmed set (`browser_trim`, on by default in bundled mode). Chosen to cut work the bridge never needs without
# changing anything a page can observe about the browser: a fingerprint of ~60 page-visible properties (globals,
# navigator, userAgentData, WebGL, canvas, EME, permissions, media, screen) is identical with and without it, and
# chatgpt.com's logged-out page still shows its input box (docs/TECHNICAL.md has the measurements). Left out on
# purpose: --disable-gpu / software GL / --force-device-scale-factor (WebGL and devicePixelRatio change),
# --disable-extensions, --disable-default-apps, --disable-component-update and MediaRouter (patchright removes
# these from Playwright's defaults because pages can tell: no Widevine, no Cast API), --enable-low-end-device-mode
# and tab discarding (would drop kept chats), --disable-site-isolation-trials (security).
TRIM_ARGS = [
    "--process-per-site",  # the bridge's tabs of one site share a renderer instead of one each
    "--disable-back-forward-cache",  # each request navigates to a fresh chat: don't keep the old page alive
    "--disable-client-side-phishing-detection",
    "--disable-domain-reliability",
    "--no-pings",
    "--metrics-recording-only",  # metrics are not uploaded
    "--disable-crash-reporter",
    "--safebrowsing-disable-download-protection",
    "--autoplay-policy=user-gesture-required",
    "--mute-audio",
]
TRIM_FEATURES = [
    "SpareRendererForSitePerProcess",  # no idle spare renderer kept warm
    "OptimizationGuideModelDownloading",
    "OptimizationHintsFetching",
    "OptimizationTargetPrediction",
    "AutofillServerCommunication",
    "CertificateTransparencyComponentUpdater",
    "InterestFeedContentSuggestions",
    "ChromeWhatsNewUI",
]


def launch_args(profile, driver: str = "patchright", trim: bool = True, window=(1180, 860)) -> list[str]:
    """The switches for one bundled launch (everything after the executable)."""
    driver = driver if driver in DRIVER_ARGS else "patchright"
    feats = list(DRIVER_FEATURES[driver]) + (TRIM_FEATURES if trim else [])
    args = list(DRIVER_ARGS[driver])
    if trim:
        args += [a for a in TRIM_ARGS if a not in args]
    return [
        f"--user-data-dir={profile}",
        "--remote-debugging-port=0",  # a free port, written to <profile>/DevToolsActivePort; 127.0.0.1 only
        "--no-startup-window",
        f"--window-size={window[0]},{window[1]}",
        # after a crash (or the watchdog's kill) Chrome offers to restore the last session in a bubble; nothing must
        # ever be drawn, and no page can tell this switch is there
        "--hide-crash-restore-bubble",
        *args,
        "--disable-features=" + ",".join(dict.fromkeys(feats)),
        "--enable-features=" + ",".join(ENABLE_FEATURES[driver]),
    ]


# ---- versions --------------------------------------------------------------------------------------------------------
def _plist(app: Path) -> dict:
    try:
        with open(Path(app) / "Contents/Info.plist", "rb") as f:
            return plistlib.load(f)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return {}


def app_version(app) -> str | None:
    return _plist(app).get("CFBundleShortVersionString")


def app_exe(app) -> Path | None:
    name = _plist(app).get("CFBundleExecutable")
    return Path(app) / "Contents/MacOS" / name if name else None


def major(version: str | None) -> int:
    m = re.match(r"\s*(\d+)", version or "")
    return int(m.group(1)) if m else 0


def vkey(version: str | None) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", version or ""))


def installed_chrome_version() -> str | None:
    return app_version(CHROME_APP)


def profile_version(profile) -> str | None:
    """The Chrome version that last opened this profile (Chrome writes it to <profile>/Last Version)."""
    try:
        return (Path(profile) / "Last Version").read_text().strip() or None
    except (OSError, TypeError):
        return None


def required_major(profile=None) -> int:
    return max(major(installed_chrome_version()), major(profile_version(profile)) if profile else 0)


def platform_key() -> str | None:
    if sys.platform != "darwin":
        return None
    return "mac-arm64" if platform.machine() == "arm64" else "mac-x64"


def root_dir() -> Path:
    return Path(env("BROWSER_DIR") or Path(env("HOME") or Path.home() / ".tierlm") / "browser").expanduser()


# ---- preparing a copy ----------------------------------------------------------------------------------------------
def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kw.pop("timeout", 300), **kw)


def _clone(src: Path, dst: Path):
    """APFS clone (no extra disk); a plain copy across volumes."""
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if _run(["cp", "-cR", str(src), str(dst)]).returncode != 0:
        shutil.rmtree(dst, ignore_errors=True)
        r = _run(["ditto", str(src), str(dst)])
        if r.returncode != 0:
            raise OSError(f"copy failed: {r.stderr.strip()[:200]}")


def make_background_copy(src_app, dst_app) -> Path:
    """dst_app: a clone of src_app whose Info.plist says LSBackgroundOnly (no Dock icon, never the active app), with
    Google's updater keys removed, re-signed ad hoc. Returns its executable. src_app is not modified."""
    src_app, dst_app = Path(src_app), Path(dst_app)
    _clone(src_app, dst_app)
    _run(["xattr", "-cr", str(dst_app)])  # Finder info / quarantine would make codesign refuse or Gatekeeper ask
    info = dst_app / "Contents/Info.plist"
    with open(info, "rb") as f:
        pl = plistlib.load(f)
    pl["LSBackgroundOnly"] = True
    pl.pop("LSUIElement", None)  # fails at launch on Chrome ("icudtl.dat not found")
    for k in ("KSUpdateURL", "KSProductID", "KSChannelID", "KSChannelID-full", "KSVersion"):
        pl.pop(k, None)
    with open(info, "wb") as f:
        plistlib.dump(pl, f)
    r = _run(["codesign", "--force", "--sign", "-", str(dst_app)])
    if r.returncode != 0:
        raise OSError(f"codesign failed: {r.stderr.strip()[:200]}")
    exe = app_exe(dst_app)
    if not exe or not exe.exists():
        raise OSError(f"no executable in {dst_app}")
    return exe


def _works(exe: Path) -> bool:
    """`--version` starts the binary without a window: catches a bundle the kernel kills (bad signature)."""
    try:
        r = _run([str(exe), "--version"], timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and bool(re.search(r"\d+\.\d+\.\d+", r.stdout))


def _apps_in(d: Path) -> list[Path]:
    try:
        return sorted(p for p in Path(d).glob("*.app") if app_exe(p))
    except OSError:
        return []


def local_sources() -> list[Path]:
    """Chromium-family apps already on this Mac that a copy can be made from (never the installed Google Chrome)."""
    out = []
    if env("BROWSER_APP"):  # an .app, or the folder that holds one (Provenance.app passes its Resources/browser)
        given = Path(env("BROWSER_APP")).expanduser()
        out += [given] if given.suffix == ".app" else _apps_in(given)
    for d in SHIPPED:
        out += _apps_in(d)
    for d in sorted(PLAYWRIGHT_CACHE.glob("chromium-*")) if PLAYWRIGHT_CACHE.exists() else []:
        if (d / "INSTALLATION_COMPLETE").exists():
            for sub in d.glob("chrome-mac*"):
                out += _apps_in(sub)
    return [p for p in out if app_version(p) and p.resolve() != CHROME_APP.resolve()]


def choose(versions: list[str], need: int) -> str | None:
    """The build to use among `versions`: the one closest to `need` from above (newest within that major); with
    no requirement (no installed Chrome, a new profile), the newest."""
    if need <= 0:
        return max(versions, key=vkey, default=None)
    ok = [v for v in versions if major(v) >= need]
    if not ok:
        return None
    best = min(major(v) for v in ok)
    return max((v for v in ok if major(v) == best), key=vkey)


def cft_download_url(
    need: int, plat: str, index: dict | None = None, stable: str | None = None
) -> tuple[str, str] | None:
    """(version, url) of the Chrome for Testing build to download for `need`, from Google's per-milestone index.
    With no requirement: the current Stable channel's milestone (not Canary, the newest in the index)."""
    if index is None:
        with urllib.request.urlopen(CFT_INDEX, timeout=30) as r:
            index = json.load(r)
    if need <= 0:
        if stable is None:
            with urllib.request.urlopen(CFT_CHANNELS, timeout=30) as r:
                stable = json.load(r)["channels"]["Stable"]["version"]
        need = major(stable)
    urls = {}
    for m in (index.get("milestones") or {}).values():
        for d in (m.get("downloads") or {}).get("chrome") or []:
            if d.get("platform") == plat:
                urls[m["version"]] = d["url"]
    v = choose(list(urls), need)
    return (v, urls[v]) if v else None


def _download(url: str, dest: Path) -> Path:
    """Fetch and unpack a Chrome for Testing zip into dest; checks the md5 the bucket reports. Returns the .app."""
    dest.mkdir(parents=True, exist_ok=True)
    zpath = dest / "download.zip"
    md5 = hashlib.md5()
    with urllib.request.urlopen(url, timeout=60) as r, open(zpath, "wb") as f:
        want = None
        for h in r.headers.get_all("x-goog-hash") or []:
            if h.startswith("md5="):
                want = base64.b64decode(h[4:]).hex()
        try:
            total = int(r.headers.get("Content-Length") or 0) or None
        except ValueError:
            total = None
        _progress(stage="download", done=0, total=total)
        done = 0
        while chunk := r.read(1 << 20):
            md5.update(chunk)
            f.write(chunk)
            done += len(chunk)
            _progress(done=done)
    if want and md5.hexdigest() != want:
        raise OSError("download corrupted (md5 mismatch)")
    _progress(stage="unpack")
    out = dest / "unpacked"
    r = _run(["ditto", "-x", "-k", str(zpath), str(out)])  # ditto keeps the framework symlinks a zipfile would lose
    zpath.unlink(missing_ok=True)
    if r.returncode != 0:
        raise OSError(f"unzip failed: {r.stderr.strip()[:200]}")
    apps = [a for d in [out, *out.glob("*")] for a in _apps_in(d)]
    if not apps:
        raise OSError("no .app in the download")
    return apps[0]


_lock = threading.Lock()


@contextlib.contextmanager
def _locked(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with _lock, open(root / ".lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)  # several bridges (or processes) preparing at once: one does it
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def current(root: Path | None = None, need: int = 0) -> dict | None:
    """The prepared copy recorded in <root>/current.json, if it is still there, works and is new enough."""
    root = Path(root) if root else root_dir()
    try:
        rec = json.loads((root / "current.json").read_text())
    except (OSError, ValueError):
        return None
    exe = Path(rec.get("exe") or "")
    if not exe.is_file() or not os.access(exe, os.X_OK) or major(rec.get("version")) < need:
        return None
    if app_version(rec.get("app") or "") != rec.get("version"):
        return None
    return rec


def _prepare_from(src_app: Path, root: Path, source: str) -> dict:
    v = app_version(src_app)
    d = root / v
    plain = d / "plain" / src_app.name
    if src_app.resolve() != plain.resolve():
        _clone(src_app, plain)
        _run(["xattr", "-cr", str(plain)])
    bg = d / "background" / src_app.name
    exe = make_background_copy(plain, bg)
    if not _works(exe):
        raise OSError(f"the prepared copy of {v} does not start")
    return _record(root, {"version": v, "exe": str(exe), "app": str(bg), "plain_app": str(plain), "source": source})


def _record(root: Path, rec: dict) -> dict:
    rec = {**rec, "prepared_at": int(time.time())}
    tmp = root / "current.json.tmp"
    tmp.write_text(json.dumps(rec, indent=1))
    tmp.replace(root / "current.json")
    _prune(root, keep=rec["version"])
    return rec


def _in_use(path: Path) -> bool:
    r = _run(["pgrep", "-f", str(path)], timeout=10)
    return bool(r.stdout.strip())


def _prune(root: Path, keep: str):
    """Remove older prepared versions that are not running."""
    for d in root.iterdir():
        if d.is_dir() and re.fullmatch(r"\d+(\.\d+)+", d.name) and d.name != keep and not _in_use(d):
            shutil.rmtree(d, ignore_errors=True)


def ensure(profile=None, root=None, download: bool = True, index: dict | None = None) -> dict | None:
    """A usable bundled browser for `profile`: {"exe", "version", "app", "plain_app", "source"}, preparing one if
    needed (a local build, else a download when `download`). None when nothing suitable can be had: the caller then
    uses the installed Chrome as before."""
    plat = platform_key()
    if not plat:
        return None
    root = Path(root) if root else root_dir()
    need = required_major(profile)

    def good(rec):  # new enough, and of the same major version when that is known
        return rec and (not need or major(rec["version"]) == need)

    rec = current(root, need)
    if good(rec):
        return rec
    with _locked(root):
        rec = current(root, need)  # another bridge may have just prepared it
        if good(rec):
            return rec
        _progress(preparing=True, stage="look", done=0, total=None, error=None, need=need, started_at=time.time())
        try:
            out = _ensure_locked(root, need, plat, rec, download, index)
            if not out:
                _progress(error="No browser for this Mac could be found to download. It will be tried again later.")
            return out
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            print(f"bundled browser: could not prepare one ({e})", file=sys.stderr)
            downloading = PROGRESS.get("stage") in ("look", "download")
            _progress(
                error="The browser could not be downloaded. Check your internet connection; it will be tried again."
                if downloading
                else "The browser could not be set up on this Mac. It will be tried again later.",
                error_detail=str(e)[:300],
            )
            return rec  # new enough but of a newer major, or None
        finally:
            _progress(preparing=False, stage=None, finished_at=time.time())


def _ensure_locked(root: Path, need: int, plat: str, rec, download: bool, index) -> dict | None:
    local = {v: a for a in local_sources() if (v := app_version(a))}
    v = choose(list(local), need)
    if v and (major(v) == need or not need):
        _progress(stage="copy", version=v)
        return _prepare_from(local[v], root, f"local:{local[v]}")
    hit = cft_download_url(need, plat, index) if download else None
    if hit and (major(hit[0]) == need or not rec):
        _progress(version=hit[0])
        with tempfile.TemporaryDirectory(dir=root, prefix=".dl-") as tmp:
            app = _download(hit[1], Path(tmp))
            dst = root / hit[0] / "plain" / app.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                shutil.rmtree(dst)
            shutil.move(str(app), str(dst))
        _progress(stage="copy")
        return _prepare_from(dst, root, f"download:{hit[1]}")
    if v and not rec:  # only newer majors around: still better than nothing
        _progress(stage="copy", version=v)
        return _prepare_from(local[v], root, f"local:{local[v]}")
    return rec


def ensure_chrome_clone(profile=None, root=None) -> dict | None:
    """`browser_source: chrome`: a background-only APFS clone of the installed Google Chrome instead of Chrome for
    Testing. It is the very same build, so pages see exactly what they see from that Chrome (Chrome for Testing
    reports no "Google Chrome" brand in navigator.userAgentData), and there is never a version gap with the
    profile; it is re-cloned whenever Chrome updates (instant, no download, ~0 disk). This Mac only: Google Chrome
    cannot be redistributed inside Provenance.app. The installed Chrome itself is never modified."""
    if not platform_key():
        return None
    v = installed_chrome_version()
    if not v or (profile and major(profile_version(profile)) > major(v)):
        return None
    croot = (Path(root) if root else root_dir()) / "chrome"
    rec = current(croot)
    if rec and rec["version"] == v:
        return rec
    with _locked(croot):
        rec = current(croot)
        if rec and rec["version"] == v:
            return rec
        try:
            exe = make_background_copy(CHROME_APP, croot / v / "background" / CHROME_APP.name)
            if not _works(exe):
                raise OSError(f"the clone of Chrome {v} does not start")
            app = str(exe.parent.parent.parent)
            return _record(croot, {"version": v, "exe": str(exe), "app": app, "plain_app": str(CHROME_APP),
                                   "source": f"clone:{CHROME_APP}"})  # fmt: skip
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            print(f"bundled browser: could not clone Chrome ({e})", file=sys.stderr)
            return None


_bg: threading.Thread | None = None
RETRY_S = 300.0  # after a failed preparation, wait this long before ensure_soon tries again (prepare() always does)
# What the last or current preparation is doing, for GET /web/accounts: stage look | download | unpack | copy,
# bytes done / total while downloading, and the last error a user can read.
PROGRESS: dict = {"preparing": False, "stage": None, "done": 0, "total": None, "error": None, "version": None}


def _progress(**changes):
    PROGRESS.update(changes)


def preparing() -> bool:
    return bool(_bg and _bg.is_alive()) or bool(PROGRESS.get("preparing"))


def prep_status() -> dict:
    """The preparation's progress: {preparing, stage, progress (0-1 while downloading, else null), done, total,
    error, version}."""
    p = dict(PROGRESS)
    p["preparing"] = preparing()
    total = p.get("total")
    p["progress"] = round(min(1.0, p["done"] / total), 3) if p["preparing"] and total else None
    return p


def prepare(profile=None, root=None) -> bool:
    """Start preparing one in a background thread unless that is already happening. False when this Mac cannot
    run one (not macOS)."""
    global _bg
    if not platform_key():
        return False
    if not (_bg and _bg.is_alive()):
        PROGRESS.update(error=None)
        _bg = threading.Thread(target=ensure, args=(profile, root), daemon=True, name="bundled-browser")
        _bg.start()
    return True


def ensure_soon(profile=None, root=None) -> dict | None:
    """What is ready now (no waiting); prepares one in a background thread when it is missing or too old (not again
    within RETRY_S of a failed attempt: a request must not start a 200 MB download every time)."""
    rec = current(Path(root) if root else None, required_major(profile))
    if rec:
        return rec
    failed = PROGRESS.get("error") and time.time() - (PROGRESS.get("finished_at") or 0) < RETRY_S
    if not failed:
        prepare(profile, root)
    return None


def signin_app(profile, mode: str = "chrome", source: str = "cft", fallback: bool = True) -> Path | None:
    """The app to open a normal, visible sign-in window with on `profile`: the same build the bridge runs there in
    the background, as a normal app, so the profile is read and written by one binary. bundled / auto: the bundled
    build's plain copy (or, for browser_source chrome, the installed Chrome it is a clone of). chrome: the installed
    Chrome, unless the bundled build has written the profile with a newer version (login_app). The installed
    Chrome is the `fallback` when no bundled build is prepared. Never an app older than the profile's Last
    Version. None when nothing suitable is there (yet)."""
    need = major(profile_version(profile))
    cands = []
    if mode in ("bundled", "auto") and source != "chrome":
        rec = current(need=required_major(profile))
        if rec and rec.get("plain_app"):
            cands.append(Path(rec["plain_app"]))
        if fallback:
            cands.append(CHROME_APP)
    elif mode in ("bundled", "auto"):
        cands.append(CHROME_APP)
    else:
        cands += [p for p in [login_app(profile), CHROME_APP] if p]
    for app in cands:
        if app_exe(app) and app_exe(app).exists() and major(app_version(app)) >= need:
            return app
    return None


def login_app(profile) -> Path | None:
    """For `weblogin`: a normal (not background-only) window must be used to sign in. That is the installed Chrome,
    unless the profile was last written by a newer browser than it (the bundled one): then the bundled build's plain
    copy, so the profile is not opened by an older Chrome."""
    pv = profile_version(profile)
    if not pv or major(pv) <= major(installed_chrome_version()):
        return None
    rec = current(need=major(pv))
    return Path(rec["plain_app"]) if rec and Path(rec["plain_app"]).exists() else None


def window_pid(profile) -> int | None:
    """The browser process that has `profile` open, from the lock Chrome keeps in it (<profile>/SingletonLock, a
    symlink to "<host>-<pid>"); None when no live process holds it."""
    try:
        target = os.readlink(Path(profile) / "SingletonLock")
        pid = int(target.rsplit("-", 1)[1])
    except (OSError, ValueError, IndexError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    return pid


def disk_usage(root=None) -> int:
    r = _run(["du", "-sk", str(Path(root) if root else root_dir())], timeout=60)
    try:
        return int(r.stdout.split()[0]) * 1024
    except (IndexError, ValueError):
        return 0


if __name__ == "__main__":  # python -m provenance_core.browser_host [--no-download | --chrome]: prepare, print it
    rec = ensure_chrome_clone() if "--chrome" in sys.argv else ensure(download="--no-download" not in sys.argv)
    print(json.dumps(rec, indent=1) if rec else "no bundled browser could be prepared")
    sys.exit(0 if rec else 1)
