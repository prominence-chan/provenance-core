import pytest


@pytest.fixture(autouse=True)
def _ledger_in_tmp(tmp_path, monkeypatch):
    """Keep the usage ledger out of ~/.tierlm during tests."""
    monkeypatch.setenv("PCORE_USAGE_LEDGER", str(tmp_path / "usage-ledger.jsonl"))
