from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from taskforge.config import env
from taskforge.core import services
from taskforge.core.models import Outbox, Run
from taskforge.core.tasks import execute_run
from taskforge.domain import State

pytestmark = pytest.mark.django_db


def submit(job, payload=None):
    return services.submit(job, payload or {}, None, "a" * 32)[0]


def test_execution_records_result_attempt_and_correlated_logs(job):
    run = submit(job, {"value": "worker says hello"})
    execute_run(str(run.id))
    run.refresh_from_db()
    assert run.state == State.SUCCEEDED
    assert run.result["value"] == "worker says hello"
    assert run.attempts.get().state == State.SUCCEEDED
    assert set(run.logs.values_list("trace_id", flat=True)) == {"a" * 32}
    assert run.logs.filter(event="demo.output", message="worker says hello").exists()
    execute_run(str(run.id))
    assert run.attempts.count() == 1


def test_claim_fences_duplicates_and_stale_completion(job):
    run = submit(job)
    claimed = services.claim(run.id)
    assert services.claim(run.id) is None
    services.finish(run.id, claimed.lease_token, error="crashed")
    assert not services.finish(run.id, claimed.lease_token, result={"wrong": True})
    run.refresh_from_db()
    assert run.state == State.RETRYING


def test_retry_exhaustion_dlq_and_manual_requeue(job):
    job.retry_policy = {"max_attempts": 2, "base_delay": 0, "jitter": 0}
    job.save()
    run = submit(job, {"fail_until": 2})
    execute_run(str(run.id))
    run.refresh_from_db()
    assert run.state == State.RETRYING
    assert Outbox.objects.get(run=run).available_at <= timezone.now()
    run.move_to(State.QUEUED)
    run.save()
    execute_run(str(run.id))
    run.refresh_from_db()
    assert run.state == State.DEAD
    assert run.attempt_count == 2
    assert "transient" in run.last_error
    assert not Outbox.objects.filter(run=run).exists()
    services.requeue(run.id)
    run.refresh_from_db()
    assert run.state == State.QUEUED
    assert run.cycle_attempt_count == 0
    assert run.attempts.count() == 2


def test_timeout_kills_task_and_records_failure(job):
    job.task_type, job.timeout_seconds = "sleep", 1
    job.retry_policy = {"max_attempts": 1}
    job.save()
    run = submit(job, {"seconds": 10})
    execute_run(str(run.id))
    run.refresh_from_db()
    assert run.state == State.DEAD
    assert "timeout" in run.last_error


def test_expired_lease_recovers_without_old_worker_overwriting(job):
    run = submit(job)
    claimed = services.claim(run.id)
    Run.objects.filter(pk=run.id).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    assert services.maintain()["recovered"] == 1
    run.refresh_from_db()
    assert run.state == State.RETRYING
    assert run.attempts.get().finished_at is not None
    assert not services.finish(run.id, claimed.lease_token, result={})


def test_broker_failure_preserves_outbox(job, monkeypatch):
    run = submit(job)
    monkeypatch.setattr(env, "redis_url", "redis://localhost:6379/0")
    with patch("taskforge.core.tasks.execute_run.apply_async", side_effect=ConnectionError):
        assert not services.dispatch_one(run.id)
    assert Outbox.objects.get(run=run).published_at is None
    run.refresh_from_db()
    assert run.state == State.QUEUED


def test_backoff_is_persisted_and_not_dispatched_early(job, monkeypatch):
    job.retry_policy = {"max_attempts": 3, "base_delay": 20, "max_delay": 30, "jitter": 0}
    job.save()
    run = submit(job)
    claimed = services.claim(run.id)
    before = timezone.now()
    services.finish(run.id, claimed.lease_token, error="temporary")
    run.refresh_from_db()
    assert 20 <= (run.next_attempt_at - before).total_seconds() < 22
    monkeypatch.setattr(env, "redis_url", "redis://localhost:6379/0")
    with patch("taskforge.core.tasks.execute_run.apply_async") as publish:
        assert not services.dispatch_one(run.id)
        publish.assert_not_called()
