import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import docker
import pytest
from redis import Redis

pytestmark = pytest.mark.integration


def test_cli_api_queue_worker_logs_golden_path(
    api, make_job, worker, dispatcher, wait_run, base_url, tmp_path
):
    job = make_job(payload_template={"value": "golden path"})
    key = api.post("/api/api-keys", json={"name": "cli"}).json()["key"]
    env = {
        **os.environ,
        "TASKFORGE_URL": base_url,
        "TASKFORGE_API_KEY": key,
        "TASKFORGE_CONFIG": str(tmp_path / "credentials.json"),
    }
    submitted = subprocess.run(
        [sys.executable, "-m", "taskforge.cli", "submit", job["id"], "--idempotency-key", "golden"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    run_id = json.loads(submitted.stdout)["id"]
    run = wait_run(run_id)
    assert run["state"] == "SUCCEEDED"
    assert run["result"]["value"] == "golden path"
    logs = api.get(f"/api/runs/{run_id}/logs").json()["results"]
    assert any(line["event"] == "demo.output" for line in logs)
    assert {line["trace_id"] for line in logs} == {run["trace_id"]}
    output = subprocess.run(
        [sys.executable, "-m", "taskforge.cli", "logs", "--run", run_id, "--follow"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "golden path" in output.stdout


def test_submit_publishes_real_redis_message_and_duplicate_race(api, make_job):
    job = make_job()
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(
            pool.map(
                lambda _: api.post(
                    f"/api/jobs/{job['id']}/submit", json={}, headers={"Idempotency-Key": "same"}
                ),
                range(6),
            )
        )
    assert all(response.status_code == 202 for response in responses)
    assert len({response.json()["id"] for response in responses}) == 1
    assert time.monotonic() - started < 5
    with Redis.from_url(os.environ["REDIS_URL"]) as redis:
        assert redis.llen("taskforge") >= 1
    distinct = api.post(
        f"/api/jobs/{job['id']}/submit", json={}, headers={"Idempotency-Key": "other"}
    )
    assert distinct.json()["id"] != responses[0].json()["id"]


def test_retry_exhaustion_dlq_and_requeue(api, make_job, worker, dispatcher, wait_run):
    job = make_job(
        payload_template={"fail_until": 5},
        retry_policy={"max_attempts": 2, "base_delay": 0.1, "max_delay": 1, "jitter": 0},
    )
    run = api.post(f"/api/jobs/{job['id']}/submit", json={}).json()
    dead = wait_run(run["id"])
    assert dead["state"] == "DEAD"
    assert dead["attempt_count"] == 2
    attempts = api.get(f"/api/runs/{run['id']}/attempts").json()
    assert len(attempts) == 2
    assert all(attempt["state"] == "FAILED" for attempt in attempts)
    assert api.post(f"/api/runs/{run['id']}/requeue", json={}).status_code == 202
    dead = wait_run(run["id"])
    assert dead["attempt_count"] == 4


def test_transient_failure_recovers(api, make_job, worker, dispatcher, wait_run):
    job = make_job(
        payload_template={"fail_until": 1},
        retry_policy={"max_attempts": 3, "base_delay": 0.1, "jitter": 0},
    )
    run = api.post(f"/api/jobs/{job['id']}/submit", json={}).json()
    result = wait_run(run["id"])
    assert result["state"] == "SUCCEEDED"
    assert result["attempt_count"] == 2


def test_worker_kill_recovers_expired_run(api, make_job, worker_factory, dispatcher, wait_run):
    job = make_job(
        task_type="sleep",
        payload_template={"seconds": 2},
        timeout_seconds=4,
        retry_policy={"max_attempts": 2, "base_delay": 0, "jitter": 0},
    )
    with worker_factory(1) as worker:
        run = api.post(f"/api/jobs/{job['id']}/submit", json={}).json()
        wait_run(run["id"], states=("RUNNING",))
        worker.kill()
        worker.wait(timeout=5)
    with worker_factory(1):
        result = wait_run(run["id"], timeout=30)
    assert result["state"] == "SUCCEEDED"
    assert result["attempt_count"] == 2
    assert "lease expired" in api.get(f"/api/runs/{run['id']}/attempts").json()[0]["error"]


def test_redis_outage_keeps_submission_durable(api, make_job, dispatcher, worker_factory, wait_run):
    job = make_job()
    with closing(docker.from_env()) as docker_client:
        redis = docker_client.containers.get(os.environ["REDIS_CONTAINER_ID"])
        redis.stop(timeout=1)
        try:
            assert api.get("/healthz").status_code == 503
            started = time.monotonic()
            response = api.post(
                f"/api/jobs/{job['id']}/submit", json={}, headers={"Idempotency-Key": "outage"}
            )
            assert response.status_code == 202, response.text
            assert time.monotonic() - started < 8
        finally:
            redis.start()
    with worker_factory():
        run = wait_run(response.json()["id"], timeout=40)
        assert run["state"] == "SUCCEEDED"


def test_timeout_is_enforced_in_real_worker(api, make_job, worker, dispatcher, wait_run):
    job = make_job(
        task_type="sleep",
        payload_template={"seconds": 10},
        timeout_seconds=1,
        retry_policy={"max_attempts": 1},
    )
    run = api.post(f"/api/jobs/{job['id']}/submit", json={}).json()
    result = wait_run(run["id"])
    assert result["state"] == "DEAD"
    assert "timeout" in result["last_error"]


@pytest.mark.load
def test_concurrency_improves_throughput_without_duplicate_attempts(
    api, make_job, worker_factory, wait_run
):
    job = make_job(task_type="sleep", payload_template={"seconds": 0.8}, timeout_seconds=5)

    def measure(concurrency):
        with worker_factory(concurrency):
            started = time.monotonic()
            runs = [
                api.post(f"/api/jobs/{job['id']}/submit", json={}).json()["id"] for _ in range(6)
            ]
            for run_id in runs:
                result = wait_run(run_id)
                assert result["state"] == "SUCCEEDED"
                assert result["attempt_count"] == 1
            return time.monotonic() - started

    serial, concurrent = measure(1), measure(4)
    assert concurrent < serial * 0.8, f"serial={serial:.2f}s concurrent={concurrent:.2f}s"
