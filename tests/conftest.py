import os
import tempfile

import pytest

# No test reads or writes the real router home (~/.tierlm: its keys, settings, ledgers): PCORE_HOME is a scratch
# directory unless a test sets its own.
os.environ["PCORE_HOME"] = tempfile.mkdtemp(prefix="pcore-test-home-")
os.environ.pop("TIERLM_HOME", None)


@pytest.fixture(autouse=True)
def _no_keys_left_over():
    """Keys one test set from "the app" (pool_keys keeps them in the process) do not reach the next."""
    from provenance_core.keys import pool_keys

    yield
    pool_keys.EXTRA.clear()
    pool_keys.REJECTED.clear()


@pytest.fixture(autouse=True)
def _ledger_in_tmp(tmp_path, monkeypatch):
    """Keep the usage ledger out of ~/.tierlm during tests."""
    monkeypatch.setenv("PCORE_USAGE_LEDGER", str(tmp_path / "usage-ledger.jsonl"))


@pytest.fixture(autouse=True)
def _no_real_credential_store(monkeypatch):
    """Tests never reach the system's credential store (the macOS Keychain): a store in memory stands in for it."""
    from provenance_core.web import credstore

    store = credstore.MemoryStore()
    credstore.use(store)
    yield store
    credstore.use(None)
