import contextlib
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]


def wait_for(check, *, timeout=30):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (httpx.HTTPError, OSError) as exc:
            last_error = exc
        time.sleep(0.15)
    raise AssertionError(f"Condition did not become true in {timeout}s; last error: {last_error}")


@pytest.fixture(scope="session")
def process_factory(tmp_path_factory):
    output = tmp_path_factory.mktemp("process-logs")

    @contextlib.contextmanager
    def launch(name, args):
        path = output / f"{name}-{uuid.uuid4().hex[:6]}.log"
        with path.open("w", encoding="utf-8") as log:
            proc = subprocess.Popen(
                [sys.executable, *args],
                cwd=ROOT,
                env=os.environ.copy(),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                yield proc, path
            except BaseException:
                log.flush()
                print(
                    f"\n{name} output:\n{path.read_text(encoding='utf-8', errors='replace')[-16000:]}"
                )
                raise
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)

    return launch


@pytest.fixture(scope="session")
def base_url(process_factory):
    assert os.environ.get("REDIS_CONTAINER_ID"), "Use python scripts/integration.py"
    with socket.socket() as socket_:
        socket_.bind(("127.0.0.1", 0))
        port = socket_.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    with process_factory("api", ["manage.py", "runserver", f"127.0.0.1:{port}", "--noreload"]) as (
        proc,
        path,
    ):
        wait_for(lambda: httpx.get(url + "/healthz").status_code == 200)
        yield url


@pytest.fixture
def api(base_url):
    with httpx.Client(base_url=base_url, timeout=15) as client:
        values = {
            "email": f"test-{uuid.uuid4().hex}@example.com",
            "password": "integration-passphrase-482",
        }
        assert client.post("/api/auth/register", json=values).status_code == 201
        response = client.post("/api/auth/login", json=values)
        assert response.status_code == 200
        client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        yield client


@pytest.fixture
def make_job(api):
    def create(**values):
        project = api.post("/api/projects", json={"name": "project-" + uuid.uuid4().hex}).json()
        response = api.post(
            f"/api/projects/{project['id']}/jobs", json={"name": "integration-job", **values}
        )
        assert response.status_code == 201, response.text
        return response.json()

    return create


@pytest.fixture
def worker_factory(process_factory):
    @contextlib.contextmanager
    def start(concurrency=4):
        with process_factory(
            "worker",
            [
                "-m",
                "celery",
                "-A",
                "taskforge.celery:app",
                "worker",
                "--pool=threads",
                f"--concurrency={concurrency}",
                "--loglevel=INFO",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
                f"--hostname=test-{uuid.uuid4().hex[:6]}@%h",
            ],
        ) as (proc, path):
            wait_for(lambda: "ready." in path.read_text(encoding="utf-8", errors="replace"))
            yield proc

    return start


@pytest.fixture
def worker(worker_factory):
    with worker_factory() as proc:
        yield proc


@pytest.fixture
def dispatcher(process_factory):
    with process_factory("dispatcher", ["manage.py", "dispatch"]) as (proc, path):
        yield proc


@pytest.fixture
def wait_run(api):
    def wait(run_id, states=("SUCCEEDED", "DEAD"), timeout=30):
        def check():
            response = api.get(f"/api/runs/{run_id}")
            assert response.status_code == 200, response.text
            data = response.json()
            return data if data["state"] in states else None

        return wait_for(check, timeout=timeout)

    return wait
