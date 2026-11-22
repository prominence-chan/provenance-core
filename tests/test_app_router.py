"""What Provenance.app needs from the router it runs itself: catalog.app.yaml (`extends:`), settings changed from the
app (a web chat on or off, its daily cap) kept in <PCORE_HOME>/settings.json, API keys in <PCORE_HOME>/keys.json,
/health, sources that say what they need, bringing a sign-in window forward, and `serve --watch-stdin`. No browser
is started here."""

import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from provenance_core import chatgpt_web as cw
from provenance_core import pool_keys
from provenance_core.catalog import load, overlay
from provenance_core.router import Router
from provenance_core.server import create_app
from provenance_core.state import State

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PCORE_HOME", str(tmp_path))
    monkeypatch.delenv("PCORE_CHATGPT_WEB", raising=False)
    monkeypatch.delenv("TIERLM_CHATGPT_WEB", raising=False)
    for v in ("TEST_KEY", "TEST_ACCT"):
        monkeypatch.delenv(v, raising=False)
    pool_keys.FROM_FILE.clear()
    yield tmp_path
    for v in list(pool_keys.FROM_FILE):
        os.environ.pop(v, None)
    pool_keys.FROM_FILE.clear()


def catalog(tmp_path, **web) -> Path:
    cat = {
        "tiers": [{"name": "nova", "min_score": 90}, {"name": "spark", "min_score": 0}],
        "providers": {
            "keyed": {"base_url": "https://api.example/${TEST_ACCT}/v1", "api_key_env": "TEST_KEY"},
            "chatgpt-web": {
                "type": "chatgpt_web",
                "base_url": "http://127.0.0.1:9",
                "api_key_env": "none",
                "web": {"notify": False, "accounts": ["main"], "profile": str(tmp_path / "profile"), **web},
            },
            "other-web": {
                "type": "chatgpt_web",
                "base_url": "http://127.0.0.1:9",
                "api_key_env": "none",
                "web": {"notify": False, "share": "chatgpt-web", "accounts": ["main"], "name": "Other Chat", **web},
            },
        },
        "models": [
            {"provider": "keyed", "model": "big", "score": 95},
            {"provider": "keyed", "model": "small", "score": 20},
            {"provider": "chatgpt-web", "model": "luna", "score": 93, "label": "GPT-6 Luna"},
            {"provider": "other-web", "model": "auto", "score": 50, "label": "Other Auto"},
        ],
    }
    p = tmp_path / "catalog.yaml"
    p.write_text(yaml.safe_dump(cat))
    return p


def client(path) -> tuple[Router, TestClient]:
    router = Router(
        load(path), State(), httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    )
    return router, TestClient(create_app(router=router))


# ---- catalog.app.yaml: `extends:` and what a new user starts with ----------------------------------------------------
def test_overlay_rules():
    base = {
        "tiers": [1],
        "providers": {"a": {"base_url": "x", "web": {"accounts": ["main", "b"], "cap": 3}}, "z": {"base_url": "z"}},
        "models": [{"provider": "a", "model": "m", "score": 1}, {"id": "a/n", "provider": "a", "model": "n"}],
    }
    over = {
        "providers": {"a": {"web": {"accounts": ["main"]}}, "z": {"remove": True}},
        "models": [{"id": "a/m", "accounts": None}, {"id": "a/n", "remove": True}, {"provider": "a", "model": "o"}],
    }
    out = overlay(base, over)
    assert out["providers"] == {"a": {"base_url": "x", "web": {"accounts": ["main"], "cap": 3}}}
    assert out["models"] == [
        {"provider": "a", "model": "m", "score": 1, "id": "a/m", "accounts": None},
        {"provider": "a", "model": "o"},
    ]
    assert base["providers"]["a"]["web"]["accounts"] == ["main", "b"]  # the base is not changed


def test_app_catalog_for_a_new_user(home, monkeypatch):
    """No keys, nothing signed in, the app turned the web chats on: the chat sites are there with one account each
    and say they need a sign-in; a tier whose API models all need keys lists the pool as needing keys."""
    monkeypatch.setenv("PCORE_CHATGPT_WEB", "1")
    for n in ("OVH_AI_ENDPOINTS_ACCESS_TOKEN", "GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(n, raising=False)
    cat = load(ROOT / "catalog.app.yaml")
    owner = load(ROOT / "catalog.yaml")
    web = {n: p for n, p in cat.providers.items() if p.type == "chatgpt_web"}
    assert set(web) == {"chatgpt-web", "qwen-web", "gemini-web"} and "zai-web" in owner.providers
    assert all(p.options["accounts"] == ["main"] and p.usable for p in web.values())
    assert not cat.providers["ovh"].usable and owner.providers["ovh"].usable  # anonymous OVH only ever says 429
    assert all(m.accounts is None for m in cat.models.values() if m.provider == "gemini-web")
    assert len(cat.models) == len(owner.models)  # the model list itself is catalog.yaml's
    r = Router(cat)
    for tier, _ in cat.tiers:
        for s in r.sources(tier):
            if s["kind"] == "web":
                assert s["needs"] == "signin" and not s["available"] and not s["pinnable"]


def test_pool_needs_keys(home, tmp_path):
    router, c = client(catalog(tmp_path))
    with c:
        tiers = {t["tier"]: t["sources"] for t in c.get("/v1/sources").json()["tiers"]}
        assert [(s["id"], s["available"], s["needs"]) for s in tiers["spark"]] == [("pool", False, "keys")]
        os.environ.update(TEST_KEY="k", TEST_ACCT="a")
        pool_keys.FROM_FILE.update({"TEST_KEY", "TEST_ACCT"})  # cleaned up by the fixture
        tiers = {t["tier"]: t["sources"] for t in c.get("/v1/sources").json()["tiers"]}
        assert [(s["id"], s["available"], s["needs"]) for s in tiers["spark"]] == [("pool", True, None)]


# ---- web chats on / off and the daily cap, from the app ----------------------------------------------------------
def test_turn_a_site_off_and_on(home, tmp_path, monkeypatch):
    monkeypatch.setenv("PCORE_CHATGPT_WEB", "1")  # the app turns the bridge on as a whole
    path = catalog(tmp_path)
    router, c = client(path)
    with c:
        assert router.catalog.providers["other-web"].usable
        router.state.health["other-web/auto"] = object()  # a cooldown from before: forgotten on a change
        r = c.patch("/web/providers/other-web", json={"enabled": False})
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is False and r.json()["enabled_by"] == "app" and r.json()["name"] == "Other Chat"
        assert not router.catalog.providers["other-web"].usable and "other-web/auto" not in router.state.health
        assert {p["id"]: p["enabled"] for p in c.get("/web/accounts").json()["providers"]} == {
            "chatgpt-web": True,
            "other-web": False,
        }
    saved = (home / "settings.json").read_text()
    assert '"other-web"' in saved and "false" in saved
    assert "settings" not in path.read_text() and "enabled" not in path.read_text()  # catalog.yaml is untouched
    # kept for the next start, and still above PCORE_CHATGPT_WEB=1
    assert not load(path).providers["other-web"].usable and load(path).providers["chatgpt-web"].usable
    _, c = client(path)
    with c:
        assert c.patch("/web/providers/other-web", json={"enabled": True}).json()["enabled"] is True
    assert load(path).providers["other-web"].usable


def test_the_machine_wide_off_switch_wins(home, tmp_path, monkeypatch):
    monkeypatch.setenv("PCORE_CHATGPT_WEB", "0")
    (home / "settings.json").write_text('{"providers": {"chatgpt-web": {"enabled": true}}}')
    router, c = client(catalog(tmp_path))
    with c:
        p = c.get("/web/accounts").json()["providers"][0]
        assert p["enabled"] is False and p["enabled_by"] == "env-off"
        r = c.patch("/web/providers/chatgpt-web", json={"enabled": True})
        assert r.status_code == 409 and "PCORE_CHATGPT_WEB=0" in r.json()["detail"]
    assert cw.enabled({"user_enabled": False, "enabled": True}) is False
    monkeypatch.delenv("PCORE_CHATGPT_WEB")
    assert cw.enabled({"enabled": True}) and cw.enabled_by({"enabled": True}) == "catalog"
    assert not cw.enabled({}) and cw.enabled({"user_enabled": True})


def test_daily_cap_reaches_the_running_bridges(home, tmp_path):
    path = catalog(tmp_path, daily_cap=50)
    router, c = client(path)
    with c:
        pool = router.bridge("chatgpt-web")  # made before the change: it has its own copy of the options
        guest = router.bridge("other-web")
        assert pool.bridges["main"].o["daily_cap"] == 50
        r = c.patch("/web/providers/chatgpt-web", json={"daily_cap": 120})
        assert r.status_code == 200 and r.json()["daily_cap"] == 120
        assert pool.bridges["main"].o["daily_cap"] == 120 and pool.add("x").o["daily_cap"] == 120
        assert guest.bridges["main"].o["daily_cap"] == 50  # another site keeps its own
        acct = r.json()["accounts"][0]
        assert acct["daily_cap"] == 120
        for bad in ({"daily_cap": 0}, {"daily_cap": 2.5}, {"daily_cap": "9"}, {"enabled": "yes"}, {"min_gap_s": 1}, {}):
            r = c.patch("/web/providers/chatgpt-web", json=bad)
            assert r.status_code == 409 and r.json()["detail"], bad
        assert c.patch("/web/providers/keyed", json={"enabled": False}).status_code == 404
        assert c.patch("/web/providers/nope", json={"enabled": False}).status_code == 404
    assert load(path).providers["chatgpt-web"].options["daily_cap"] == 120


# ---- sources say what they need; a never signed-in account is skipped ------------------------------------------------
def test_unchecked_accounts_are_skipped(home, tmp_path, monkeypatch):
    monkeypatch.setenv("PCORE_CHATGPT_WEB", "1")
    router, c = client(catalog(tmp_path, skip_unchecked=True))
    with c:
        b = router.bridge("chatgpt-web").bridges["main"]
        with pytest.raises(cw.WebSkip):
            b._precheck()
        nova = {t["tier"]: t["sources"] for t in c.get("/v1/sources").json()["tiers"]}["nova"]
        luna = next(s for s in nova if s["id"] == "luna")
        assert luna["needs"] == "signin" and not luna["available"]
        b.save(signed_in=True, checked_at=int(time.time()))
        b._precheck()  # signed in once: used
        nova = {t["tier"]: t["sources"] for t in c.get("/v1/sources").json()["tiers"]}["nova"]
        luna = next(s for s in nova if s["id"] == "luna")
        assert luna["needs"] is None and luna["available"] and luna["pinnable"]


# ---- /health, bringing the sign-in window forward ---------------------------------------------------------------
def test_health(home, tmp_path):
    _, c = client(catalog(tmp_path))
    with c:
        h = c.get("/health").json()
    assert h["ok"] and h["pid"] == os.getpid() and h["home"] == str(home) and h["service"] == "provenance-core"
    assert h["api"] == {"providers": 1, "usable": 0, "models": 0} and h["tiers"] == ["nova", "spark"]


def test_show_needs_an_open_window(home, tmp_path):
    _, c = client(catalog(tmp_path))
    with c:
        r = c.post("/web/login/show", json={"provider": "chatgpt-web", "account": "main"})
        assert r.status_code == 409 and r.json()["state"] == "idle" and "no sign-in window" in r.json()["detail"]
        assert c.post("/web/login/show", json={"provider": "chatgpt-web", "account": "zz"}).status_code == 404


# ---- API keys from the app ---------------------------------------------------------------------------------------
def test_keys_from_the_app(home, tmp_path):
    router, c = client(catalog(tmp_path))
    with c:
        v = c.get("/pool/keys").json()["providers"]
        assert v == [
            {
                "id": "keyed",
                "usable": False,
                "anonymous": False,
                "vars": [
                    {"name": "TEST_KEY", "set": False, "from": None},
                    {"name": "TEST_ACCT", "set": False, "from": None},
                ],
                "models": 2,
                "tiers": ["nova", "spark"],
            }
        ]
        r = c.put("/pool/keys/keyed", json={"values": {"TEST_KEY": "  sk-secret-1  ", "TEST_ACCT": "acct"}})
        assert r.status_code == 200 and r.json()["usable"] is True
        assert "sk-secret" not in r.text and "acct" not in r.text  # values never come back
        assert os.environ["TEST_KEY"] == "sk-secret-1" and router.catalog.providers["keyed"].usable
        f = home / "keys.json"
        assert stat.S_IMODE(f.stat().st_mode) == 0o600 and "sk-secret-1" in f.read_text()
        for bad in ({"values": {"OTHER": "x"}}, {"values": {"TEST_KEY": ""}}, {"values": {"TEST_KEY": "a\nb"}}, {}):
            r = c.put("/pool/keys/keyed", json=bad)
            assert r.status_code == 409 and "sk-secret" not in r.text, bad
        assert c.put("/pool/keys/chatgpt-web", json={"values": {"X": "y"}}).status_code == 404
    # the next start reads them back
    os.environ.pop("TEST_KEY")
    pool_keys.FROM_FILE.clear()
    assert load(catalog(tmp_path)).providers["keyed"].usable and "TEST_KEY" in pool_keys.FROM_FILE
    _, c = client(catalog(tmp_path))
    with c:
        r = c.delete("/pool/keys/keyed")
        assert r.status_code == 200 and r.json()["usable"] is False and "TEST_KEY" not in os.environ
        assert "sk-secret" not in (home / "keys.json").read_text()
        os.environ["TEST_KEY"] = "from-keys-env"  # set outside the app: cannot be removed from here
        r = c.delete("/pool/keys/keyed")
        assert r.status_code == 409 and "keys.env" in r.json()["detail"] and "from-keys-env" not in r.text
    os.environ.pop("TEST_KEY")


# ---- serve --watch-stdin: the router goes when the app that started it goes -----------------------------------------
def test_watch_stdin_shuts_the_router_down(home, tmp_path):
    path = catalog(tmp_path)
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    p = subprocess.Popen(
        [sys.executable, "-m", "provenance_core", "serve", "--catalog", str(path), "--port", "0", "--watch-stdin"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        cwd=tmp_path,
    )
    try:
        out = b""
        end = time.time() + 30
        while b"Uvicorn running" not in out and time.time() < end:
            out += p.stdout.read1(4096)
        assert b"Uvicorn running" in out, out
        p.stdin.close()  # the app is gone
        code = p.wait(timeout=20)
        out += p.stdout.read()
        # a clean shutdown (uvicorn re-raises the SIGTERM it handled once it has shut down, hence -15)
        assert code in (0, -15) and b"stdin closed" in out and b"Application shutdown complete" in out, out
    finally:
        if p.poll() is None:
            p.kill()
