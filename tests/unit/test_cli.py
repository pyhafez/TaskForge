import json
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from taskforge.cli import app, load_config, save_config

runner = CliRunner()


def test_cli_auth_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKFORGE_CONFIG", str(tmp_path / "nested/credentials.json"))
    save_config({"url": "http://localhost:8000", "token": "secret"})
    assert load_config()["token"] == "secret"
    result = runner.invoke(app, ["logout"])
    assert result.exit_code == 0
    assert load_config() == {}


def test_submit_uses_rest_api_and_idempotency_header(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKFORGE_CONFIG", str(tmp_path / "credentials.json"))
    with patch("taskforge.cli.api", return_value={"id": "run-1", "state": "QUEUED"}) as api:
        result = runner.invoke(
            app, ["submit", "job-1", "--payload", '{"value":"test"}', "--idempotency-key", "abc"]
        )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["id"] == "run-1"
    api.assert_called_once_with(
        "POST",
        "/api/jobs/job-1/submit",
        body={"payload": {"value": "test"}},
        headers={"Idempotency-Key": "abc"},
    )


def test_follow_drains_all_log_pages_and_stops_on_terminal_state():
    pages = [
        {
            "results": [
                {"timestamp": "now", "level": "info", "event": "demo.output", "message": "first"}
            ],
            "next_cursor": 1,
            "has_more": True,
            "state": "SUCCEEDED",
        },
        {
            "results": [
                {"timestamp": "now", "level": "info", "event": "run.succeeded", "message": "last"}
            ],
            "next_cursor": 2,
            "has_more": False,
            "state": "SUCCEEDED",
        },
    ]
    with patch("taskforge.cli.api", side_effect=pages):
        result = runner.invoke(app, ["logs", "--run", "run-1", "--follow"])
    assert result.exit_code == 0
    assert "first" in result.stdout and "last" in result.stdout


def test_bad_json_and_failed_http_have_nonzero_exit(tmp_path, monkeypatch):
    assert runner.invoke(app, ["submit", "id", "--payload", "not-json"]).exit_code == 2
    monkeypatch.setenv("TASKFORGE_CONFIG", str(tmp_path / "credentials.json"))
    with patch("httpx.Client.request", side_effect=httpx.ConnectError("unreachable")):
        result = runner.invoke(app, ["list"])
    assert result.exit_code == 1
    assert "Connection failed" in result.output


def test_cli_login_verifies_and_saves_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKFORGE_CONFIG", str(tmp_path / "credentials.json"))
    with patch(
        "httpx.Client.get", return_value=httpx.Response(200, json={"email": "test@example.com"})
    ):
        result = runner.invoke(app, ["login", "--api-key", "tf_secret"])
    assert result.exit_code == 0
    assert load_config()["api_key"] == "tf_secret"


def test_cli_refuses_remote_plaintext_login():
    assert runner.invoke(app, ["login", "--url", "http://example.com"]).exit_code == 2
