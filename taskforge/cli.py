"""The CLI only talks to the public REST API."""

import json
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import typer

app = typer.Typer(no_args_is_help=True, help="Submit, inspect, and follow TaskForge jobs.")


def config_path():
    return Path(
        os.environ.get("TASKFORGE_CONFIG", Path.home() / ".config/taskforge/credentials.json")
    )


def load_config():
    path = config_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        typer.echo("Cannot read credentials. Run taskforge login again.", err=True)
        raise typer.Exit(1) from None


def save_config(data):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix="credentials-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def api(method, path, *, body=None, headers=None):
    config = load_config()
    url = os.environ.get("TASKFORGE_URL", config.get("url", "http://localhost:8000")).rstrip("/")
    auth = {}
    key = os.environ.get("TASKFORGE_API_KEY", config.get("api_key"))
    token = os.environ.get("TASKFORGE_TOKEN", config.get("token"))
    if key:
        auth["X-API-Key"] = key
    elif token:
        auth["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            response = client.request(
                method, url + path, json=body, headers={**auth, **(headers or {})}
            )
        if response.is_error:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text[:500]
            typer.echo(f"HTTP {response.status_code}: {detail}", err=True)
            raise typer.Exit(1)
        return response.json() if response.content else None
    except httpx.HTTPError as exc:
        typer.echo(f"Connection failed: {exc}", err=True)
        raise typer.Exit(1) from None


def output(data):
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False))


@app.command()
def login(
    url: str = typer.Option("http://localhost:8000", help="API base URL"),
    email: str | None = typer.Option(None),
    api_key: str | None = typer.Option(
        None, envvar="TASKFORGE_API_KEY", help="Prefer environment variable to avoid shell history"
    ),
):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise typer.BadParameter("Expected an HTTP(S) URL")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise typer.BadParameter("Remote authentication requires HTTPS")
    data = {"url": url.rstrip("/")}
    try:
        with httpx.Client(base_url=data["url"], timeout=10) as client:
            if api_key:
                response = client.get("/api/auth/me", headers={"X-API-Key": api_key})
                data["api_key"] = api_key
            else:
                email = email or typer.prompt("Email")
                password = typer.prompt("Password", hide_input=True)
                response = client.post(
                    "/api/auth/login", json={"email": email, "password": password}
                )
                if response.is_success:
                    data["token"] = response.json()["access_token"]
            if not response.is_success:
                typer.echo(f"Login failed (HTTP {response.status_code}).", err=True)
                raise typer.Exit(1)
        save_config(data)
        typer.echo("Credentials saved.")
    except httpx.HTTPError as exc:
        typer.echo(f"Connection failed: {exc}", err=True)
        raise typer.Exit(1) from None


@app.command()
def logout():
    config_path().unlink(missing_ok=True)
    typer.echo("Local credentials removed.")


@app.command()
def submit(
    job: str = typer.Argument(..., help="Job UUID"),
    payload: str = typer.Option("{}", help="JSON payload overrides"),
    idempotency_key: str | None = typer.Option(None),
):
    try:
        values = json.loads(payload)
    except ValueError:
        raise typer.BadParameter("Payload must be valid JSON") from None
    if not isinstance(values, dict):
        raise typer.BadParameter("Payload must be a JSON object")
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    output(api("POST", f"/api/jobs/{job}/submit", body={"payload": values}, headers=headers))


@app.command("list")
def list_resources(
    resource: str = typer.Argument("runs", help="projects, jobs, or runs"),
    page: int = typer.Option(1, min=1),
    state: str | None = typer.Option(None),
):
    if resource not in ("projects", "jobs", "runs"):
        raise typer.BadParameter("Choose projects, jobs, or runs")
    params = httpx.QueryParams({"page": page, **({"state": state} if state else {})})
    output(api("GET", f"/api/{resource}?{params}"))


@app.command()
def status(run: str):
    output(api("GET", f"/api/runs/{run}"))


@app.command()
def logs(
    run: str = typer.Option(..., "--run"),
    follow: bool = typer.Option(False, "--follow"),
    interval: float = typer.Option(1, min=0.1),
):
    cursor = 0
    try:
        while True:
            data = api("GET", f"/api/runs/{run}/logs?after={cursor}")
            for entry in data["results"]:
                typer.echo(
                    f"{entry['timestamp']} {entry['level'].upper():7} {entry['event']} {entry['message']}"
                )
            cursor = data["next_cursor"]
            if data["has_more"]:
                continue
            if not follow or data["state"] in ("SUCCEEDED", "DEAD"):
                if follow and data["state"] == "DEAD":
                    raise typer.Exit(1)
                return
            time.sleep(interval)
    except KeyboardInterrupt:
        raise typer.Exit(130) from None


@app.command()
def requeue(run: str):
    output(api("POST", f"/api/runs/{run}/requeue", body={}))


@app.command("create-project")
def create_project(name: str):
    output(api("POST", "/api/projects", body={"name": name}))


@app.command("create-job")
def create_job(project: str, name: str, task_type: str = typer.Option("python_demo")):
    output(
        api("POST", f"/api/projects/{project}/jobs", body={"name": name, "task_type": task_type})
    )


if __name__ == "__main__":
    app()
