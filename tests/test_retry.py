import pytest
import requests

from wechat_vault.store.tablestore import _retry_request


def test_transient_timeout_retries_without_masking_permanent_errors(monkeypatch):
    monkeypatch.setattr("wechat_vault.store.tablestore.time.sleep", lambda _: None)
    calls = []

    def operation():
        calls.append(1)
        if len(calls) == 1:
            raise requests.ReadTimeout("simulated transport failure")
        return "committed"

    assert _retry_request(operation) == "committed"
    assert len(calls) == 2
    with pytest.raises(ValueError):
        _retry_request(lambda: (_ for _ in ()).throw(ValueError("invalid input")))
