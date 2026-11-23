import pytest


@pytest.fixture(autouse=True)
def _ledger_in_tmp(tmp_path, monkeypatch):
    """Keep the usage ledger out of ~/.tierlm during tests."""
    monkeypatch.setenv("PCORE_USAGE_LEDGER", str(tmp_path / "usage-ledger.jsonl"))


@pytest.fixture(autouse=True)
def _no_real_credential_store(monkeypatch):
    """Tests never reach the system's credential store (the macOS Keychain): a store in memory stands in for it."""
    from provenance_core import credstore

    store = credstore.MemoryStore()
    credstore.use(store)
    yield store
    credstore.use(None)
