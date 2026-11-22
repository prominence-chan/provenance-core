"""browser_host: choosing, preparing and launching the bundled browser's copy (no browser is started here; the
bridge's bundled mode against the mock page is in test_chatgpt_web.py)."""

import json
import plistlib
import subprocess
import sys

import pytest

from provenance_core import browser_host as bh

mac = pytest.mark.skipif(sys.platform != "darwin", reason="macOS app bundles")
INDEX = {
    "milestones": {
        str(m): {
            "milestone": str(m),
            "version": v,
            "downloads": {
                "chrome": [{"platform": "mac-arm64", "url": f"https://x/{v}/mac-arm64/chrome-mac-arm64.zip"}]
            },
        }
        for m, v in [(156, "156.0.8000.1"), (157, "157.0.8103.78"), (158, "158.0.8127.36")]
    }
}


def fake_app(where, version="157.0.1.2", name="Fake Browser"):
    """A minimal app bundle whose executable answers --version like Chrome does."""
    app = where / f"{name}.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    exe = app / "Contents/MacOS" / name
    exe.write_text(f'#!/bin/sh\necho "{name} {version}"\n')
    exe.chmod(0o755)
    with open(app / "Contents/Info.plist", "wb") as f:
        plistlib.dump(
            {
                "CFBundleExecutable": name,
                "CFBundleIdentifier": "test.fake.browser",
                "CFBundleShortVersionString": version,
                "CFBundlePackageType": "APPL",
                "KSProductID": "com.google.Chrome",
            },
            f,
        )
    return app


def test_choose_the_closest_major_from_above():
    vs = ["156.0.1.0", "157.0.8103.6", "157.0.8103.78", "158.0.8127.36"]
    assert bh.choose(vs, 157) == "157.0.8103.78"  # same major as the installed Chrome, newest in it
    assert bh.choose(vs, 0) == "158.0.8127.36" and bh.choose([], 0) is None  # no requirement: the newest
    assert bh.choose(["158.0.1.0", "160.0.1.0"], 157) == "158.0.1.0"  # never older than the profile
    assert bh.choose(["153.0.8010.12"], 157) is None  # e.g. Playwright's cached build: too old for the profile
    assert bh.cft_download_url(157, "mac-arm64", INDEX)[0] == "157.0.8103.78"
    assert bh.cft_download_url(0, "mac-arm64", INDEX, stable="157.0.8103.78")[0] == "157.0.8103.78"  # not Canary
    assert bh.cft_download_url(159, "mac-arm64", INDEX) is None
    assert bh.cft_download_url(157, "linux64", INDEX) is None


def test_required_major_reads_the_profile_version(tmp_path, monkeypatch):
    monkeypatch.setattr(bh, "installed_chrome_version", lambda: "157.0.8103.80")
    assert bh.required_major(tmp_path) == 157
    (tmp_path / "Last Version").write_text("158.0.8127.36")
    assert bh.required_major(tmp_path) == 158 and bh.profile_version(tmp_path) == "158.0.8127.36"


def test_launch_switches():
    args = bh.launch_args("/p", "patchright", trim=True)
    plain = bh.launch_args("/p", "patchright", trim=False)
    assert args[:3] == ["--user-data-dir=/p", "--remote-debugging-port=0", "--no-startup-window"]
    for keep in (  # a window nobody sees must keep running timers; one cookie jar with weblogin and Playwright
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--use-mock-keychain",
        "--password-store=basic",
        "--disable-blink-features=AutomationControlled",
    ):
        assert keep in args and keep in plain
    assert set(bh.TRIM_ARGS) <= set(args) and not set(bh.TRIM_ARGS) & set(plain)
    feats = [a for a in args if a.startswith("--disable-features=")]
    assert len(feats) == 1  # Chrome only reads the last one: everything merged into it
    assert set(bh.TRIM_FEATURES) | set(bh._FEATURES_PATCHRIGHT) == set(feats[0].split("=", 1)[1].split(","))
    # nothing that changes how the browser looks to a site, or that patchright removes for that reason
    banned = (
        "--user-agent",
        "--lang",
        "--headless",
        "--disable-gpu",
        "--use-gl",
        "--use-angle",
        "--force-device-scale-factor",
        "--disable-extensions",
        "--disable-component-update",
        "--disable-default-apps",
        "--enable-automation",
        "--no-sandbox",
        "--window-position",
    )
    assert not [a for a in args if a.split("=")[0] in banned]
    assert "MediaRouter" not in feats[0]  # pages can tell (no Cast API)
    assert "--disable-blink-features=AutomationControlled" not in bh.launch_args("/p", "playwright")


@mac
def test_prepare_a_background_only_copy(tmp_path, monkeypatch):
    src = fake_app(tmp_path / "src", "157.0.1.2")
    root = tmp_path / "browser"
    monkeypatch.setattr(bh, "local_sources", lambda: [src])
    monkeypatch.setattr(bh, "installed_chrome_version", lambda: "157.0.8103.80")
    rec = bh.ensure(root=root, download=False)
    assert rec["version"] == "157.0.1.2" and rec["source"] == f"local:{src}"
    pl = plistlib.loads((root / "157.0.1.2/background/Fake Browser.app/Contents/Info.plist").read_bytes())
    assert pl["LSBackgroundOnly"] is True and "LSUIElement" not in pl and "KSProductID" not in pl
    assert subprocess.run(["codesign", "--verify", rec["app"]]).returncode == 0  # re-signed: it would be killed
    assert "LSBackgroundOnly" not in plistlib.loads((src / "Contents/Info.plist").read_bytes())  # source untouched
    assert json.loads((root / "current.json").read_text())["exe"] == rec["exe"]
    assert bh.ensure(root=root, download=False) == rec  # prepared once
    # Chrome moved on to 158: the copy is too old for profiles that Chrome writes now, and nothing newer is here
    monkeypatch.setattr(bh, "installed_chrome_version", lambda: "158.0.8127.40")
    assert bh.current(root, need=158) is None and bh.ensure(root=root, download=False) is None
    # a newer build shows up (e.g. shipped inside Provenance.app): prepared, and the old one removed
    src2 = fake_app(tmp_path / "src2", "158.0.8127.36")
    monkeypatch.setattr(bh, "local_sources", lambda: [src, src2])
    rec2 = bh.ensure(root=root, download=False)
    assert rec2["version"] == "158.0.8127.36" and not (root / "157.0.1.2").exists()


@mac
def test_chrome_clone_follows_the_installed_chrome(tmp_path, monkeypatch):
    chrome = fake_app(tmp_path / "Applications", "157.0.8103.80", "Google Chrome")
    monkeypatch.setattr(bh, "CHROME_APP", chrome)
    root, prof = tmp_path / "browser", tmp_path / "profile"
    prof.mkdir()
    rec = bh.ensure_chrome_clone(prof, root)
    assert rec["version"] == "157.0.8103.80" and rec["plain_app"] == str(chrome)
    assert plistlib.loads((chrome / "Contents/Info.plist").read_bytes()).get("LSBackgroundOnly") is None  # untouched
    assert bh.ensure_chrome_clone(prof, root) == rec
    (prof / "Last Version").write_text("158.0.1.0")  # written by a newer browser: this Chrome must not open it
    assert bh.ensure_chrome_clone(prof, root) is None
    (prof / "Last Version").write_text("157.0.8103.80")
    chrome2 = fake_app(tmp_path / "Applications2", "157.0.8103.92", "Google Chrome")  # Chrome updated itself
    monkeypatch.setattr(bh, "CHROME_APP", chrome2)
    rec2 = bh.ensure_chrome_clone(prof, root)
    assert rec2["version"] == "157.0.8103.92" and not (root / "chrome/157.0.8103.80").exists()


@mac
def test_nothing_suitable_means_none(tmp_path, monkeypatch):
    monkeypatch.setattr(bh, "local_sources", lambda: [fake_app(tmp_path, "153.0.8010.12")])
    monkeypatch.setattr(bh, "installed_chrome_version", lambda: "157.0.8103.80")
    assert bh.ensure(root=tmp_path / "b", download=False) is None  # the bridge then uses the installed Chrome


def test_weblogin_uses_a_normal_window_that_can_open_the_profile(tmp_path, monkeypatch):
    plain = tmp_path / "plain.app"
    plain.mkdir()
    monkeypatch.setattr(bh, "installed_chrome_version", lambda: "157.0.8103.80")
    monkeypatch.setattr(bh, "current", lambda root=None, need=0: {"plain_app": str(plain), "version": "158.0.1.0"})
    assert bh.login_app(tmp_path) is None  # never opened: the installed Chrome
    (tmp_path / "Last Version").write_text("157.0.8103.78")
    assert bh.login_app(tmp_path) is None  # written by the bundled 157: the installed 157 can open it
    (tmp_path / "Last Version").write_text("158.0.1.0")
    assert bh.login_app(tmp_path) == plain  # written by a newer bundled build: its normal copy, not an older Chrome
