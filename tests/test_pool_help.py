"""The key instructions (pool_help.yaml): complete for the catalog's pool, well-formed, served, and in step with
docs/pool-keys.md."""

import re
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from provenance_core import pool_help, pool_keys
from provenance_core.catalog import load
from provenance_core.router import Router
from provenance_core.server import create_app
from provenance_core.state import State

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    "id name free_tier confidence what_you_get needs time_minutes signup_url keys_url key_var steps key_format "
    "gotchas checked sources"
).split()
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@pytest.fixture(scope="module")
def catalog():
    return load(ROOT / "catalog.yaml", ROOT / "no-scores.yaml")


@pytest.fixture(scope="module")
def data():
    return pool_help.load()


def test_every_pool_provider_has_an_entry(catalog, data):
    pool = [n for n, p in catalog.providers.items() if p.type == "openai"]
    ids = [p["id"] for p in data["providers"]]
    assert len(ids) == len(set(ids))
    assert sorted(ids) == sorted(pool)


def test_entries_are_well_formed(catalog, data):
    assert DATE.match(data["checked"]) and data["assistant_notes"]
    for p in data["providers"]:
        missing = [k for k in REQUIRED if k not in p]
        assert not missing, (p.get("id"), missing)
        assert p["free_tier"] in pool_help.FREE and p["confidence"] in ("seen", "docs-only", "uncertain"), p["id"]
        assert DATE.match(p["checked"]), p["id"]
        assert isinstance(p["time_minutes"], int) and p["time_minutes"] >= 0, p["id"]
        for k in ("name", "what_you_get", "needs", "key_format"):
            assert isinstance(p[k], str) and p[k].strip(), (p["id"], k)
        for k in ("steps", "gotchas", "sources"):
            assert p[k] and all(isinstance(s, str) and s.strip() for s in p[k]), (p["id"], k)
        for u in [p["signup_url"], p["keys_url"], *p["sources"]]:
            assert u.startswith("https://") and " " not in u, (p["id"], u)
        for u in re.findall(r"http\S+", " ".join([*p["steps"], *p["gotchas"], p["what_you_get"]])):
            assert u.startswith("https://"), (p["id"], u)


def test_variables_match_the_catalog(catalog, data):
    """key_var and extra_fields name exactly the variables PUT /pool/keys/{id} takes for that provider."""
    for p in data["providers"]:
        names = [p["key_var"]] if p["key_var"] else []
        names += [f["var"] for f in p.get("extra_fields") or []]
        assert names == pool_keys.provider_vars(catalog.providers[p["id"]]), p["id"]
        for f in p.get("extra_fields") or []:
            assert f["label"].strip() and f["where"].strip(), p["id"]


def test_key_patterns_compile_and_tell_keys_apart(data):
    fake = {  # made-up values in each documented shape
        "groq": "gsk_" + "a1B2" * 13,
        "openrouter": "sk-or-v1-" + "0a1b2c3d" * 8,
        "cloudflare": "cfut_" + "Ab3d" * 12,
        "kilo": "eyJ" + "hbGciOi" * 3 + "." + "eyJzdWIi" * 4 + "." + "c2lnbmF0dXJl" * 3,
    }
    pats = {}
    for p in data["providers"]:
        for holder in [p, *(p.get("extra_fields") or [])]:
            for k in ("key_pattern", "pattern"):
                if holder.get(k):
                    re.compile(holder[k])
        if p.get("key_pattern"):
            pats[p["id"]] = re.compile(p["key_pattern"])
    assert set(pats) == set(fake)
    for pid, pat in pats.items():
        assert pat.match(fake[pid]), pid
        assert not pat.match("me@example.com") and not pat.match("https://example.com/keys"), pid
        assert not any(pat.match(v) for other, v in fake.items() if other != pid), pid
    assert pats["cloudflare"].match("x" * 40)  # a token from before the prefixes


def test_the_doc_is_generated_from_the_data(data):
    assert (ROOT / "docs" / "pool-keys.md").read_text(encoding="utf-8") == pool_help.render()
    doc = pool_help.render()
    for p in data["providers"]:
        assert f"(`{p['id']}`)" in doc and p["steps"][0] in doc


def test_route(tmp_path, monkeypatch, data):
    monkeypatch.delenv("PCORE_TOKEN", raising=False)
    monkeypatch.delenv("TIERLM_TOKEN", raising=False)
    path = tmp_path / "catalog.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "tiers": [{"name": "lo", "min_score": 0}],
                "providers": {"keyed": {"base_url": "https://keyed.test/v1", "api_key_env": "TEST_HELP_KEY"}},
                "models": [{"provider": "keyed", "model": "k", "score": 50}],
            }
        )
    )
    cat = load(path, tmp_path / "scores.yaml")
    c = TestClient(create_app(router=Router(cat, State())))
    with c:
        r = c.get("/pool/help")
        got = r.json()
        # each provider also carries how following its steps has gone so far (nothing recorded in this home)
        nothing = {"tries": 0, "worked": 0, "last_worked": None, "common_trouble": []}
        assert all(p.pop("experience") == nothing for p in got["providers"])
        assert r.status_code == 200 and got == data
        assert [p["id"] for p in r.json()["providers"]][:2] == ["kilo", "google"]
        one = c.get("/pool/help", params={"provider": "cloudflare"})
        assert one.status_code == 200 and one.json()["checked"] == data["checked"]
        assert one.json()["provider"]["extra_fields"][0]["var"] == "CLOUDFLARE_ACCOUNT_ID"
        assert one.json()["provider"]["experience"]["tries"] == 0
        missing = c.get("/pool/help", params={"provider": "nope"})
        assert missing.status_code == 404 and "nope" in missing.json()["detail"]


def test_route_takes_the_router_token(tmp_path, monkeypatch):
    monkeypatch.setenv("PCORE_TOKEN", "t0ken")
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump({"tiers": [{"name": "lo", "min_score": 0}], "providers": {}, "models": []}))
    c = TestClient(create_app(router=Router(load(path, tmp_path / "scores.yaml"), State())))
    with c:
        assert c.get("/pool/help").status_code == 401
        assert c.get("/pool/help", headers={"Authorization": "Bearer t0ken"}).status_code == 200
