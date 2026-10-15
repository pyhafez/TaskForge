import pytest

from taskforge.task_types import HTTPPayload, http_task, register, validate_task_payload


@pytest.mark.parametrize(
    "url",
    ["http://example.com", "https://127.0.0.1", "file:///etc/passwd", "https://example.com:8000"],
)
def test_http_rejects_unapproved_destinations(url, monkeypatch):
    monkeypatch.setenv("HTTP_ALLOWED_HOSTS", "example.com")
    with pytest.raises(ValueError):
        http_task(HTTPPayload(url=url), {}, lambda *args: None)


def test_registry_rejects_duplicate_names():
    with pytest.raises(ValueError):
        register("sleep", object)(lambda: None)


def test_sleep_payload_validation():
    assert validate_task_payload("sleep", {}) == {"seconds": 1}
    with pytest.raises(ValueError):
        validate_task_payload("sleep", {"seconds": -1})
    with pytest.raises(ValueError):
        validate_task_payload("arbitrary_code", {})
