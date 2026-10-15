import hashlib
import json
import uuid
from datetime import timedelta

import structlog
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from taskforge.config import env
from taskforge.core.errors import Conflict
from taskforge.core.models import Attempt, Job, Outbox, Run, RunLog
from taskforge.domain import RetryPolicy, State
from taskforge.observability import run_span, trace_headers
from taskforge.task_types import validate_task_payload

logger = structlog.get_logger()


def record_log(run, event, message="", level="info"):
    RunLog.objects.create(
        run=run,
        event=event,
        message=message[:4000],
        level=level,
        trace_id=run.trace_id,
        attempt=run.attempt_count,
    )
    getattr(logger, level)(
        event, run_id=str(run.id), trace_id=run.trace_id, attempt=run.attempt_count
    )


def submit(job, payload, key, trace_id):
    if key is not None and (not key.strip() or len(key) > 200):
        raise ValidationError({"idempotency_key": "Must contain 1–200 characters."})
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    # Lock the parent: concurrent submit, edit, and deletion serialize on PostgreSQL.
    with transaction.atomic():
        job = Job.objects.select_for_update().get(pk=job.pk)
        if key is not None:
            existing = Run.objects.filter(job=job, idempotency_key=key).first()
            if existing:
                if existing.request_fingerprint != fingerprint:
                    raise Conflict("Idempotency key was already used with a different payload.")
                return existing, False
        try:
            effective = validate_task_payload(job.task_type, {**job.payload_template, **payload})
        except ValueError as exc:
            raise ValidationError({"payload": str(exc)}) from exc
        run = Run(
            job=job,
            task_type=job.task_type,
            payload=effective,
            retry_policy=job.retry_policy,
            timeout_seconds=job.timeout_seconds,
            trace_id=trace_id,
            trace_context=trace_headers(),
            idempotency_key=key,
            request_fingerprint=fingerprint,
        )
        run.move_to(State.QUEUED)
        run.save()
        Outbox.objects.create(run=run, available_at=timezone.now())
        record_log(run, "run.submitted")
        # Broker outage cannot roll back acceptance; periodic maintenance republishes.
        transaction.on_commit(lambda: dispatch_one(run.pk), robust=True)
    return run, True


def dispatch_one(run_id):
    if not env.redis_url:
        return False
    from taskforge.core.tasks import execute_run

    try:
        with transaction.atomic():
            # Always acquire locks in run -> outbox order.
            run = Run.objects.select_for_update().filter(pk=run_id).first()
            if not run or run.state not in (State.QUEUED, State.RETRYING):
                return False
            item = (
                Outbox.objects.select_for_update()
                .filter(run=run, available_at__lte=timezone.now())
                .first()
            )
            if not item:
                return False
            if item.published_at and item.published_at > timezone.now() - timedelta(
                seconds=env.queue_republish_seconds
            ):
                return False
            if run.state == State.RETRYING:
                run.move_to(State.QUEUED)
                run.save(update_fields=["state", "updated_at"])
            with run_span("run.publish", run):
                execute_run.apply_async(
                    args=[str(run.id)],
                    headers={"trace_id": run.trace_id, **run.trace_context},
                    retry=False,
                )
            item.published_at = timezone.now()
            item.save(update_fields=["published_at"])
            logger.info("run.enqueued", run_id=str(run.id), trace_id=run.trace_id)
        return True
    except Exception:
        logger.warning("queue.publish_deferred", run_id=str(run_id))
        return False


def claim(run_id):
    with transaction.atomic():
        run = Run.objects.select_for_update().filter(pk=run_id).first()
        if not run or run.state != State.QUEUED:
            return None
        run.move_to(State.RUNNING)
        run.attempt_count += 1
        run.cycle_attempt_count += 1
        run.lease_token = uuid.uuid4()
        run.lease_expires_at = timezone.now() + timedelta(
            seconds=run.timeout_seconds + env.lease_grace_seconds
        )
        run.next_attempt_at = None
        run.save()
        Attempt.objects.create(run=run, number=run.attempt_count, lease_token=run.lease_token)
        Outbox.objects.filter(run=run).delete()
        record_log(run, "run.started")
        return run


def append_output(run_id, token, event, message):
    with transaction.atomic():
        run = (
            Run.objects.select_for_update()
            .filter(pk=run_id, state=State.RUNNING, lease_token=token)
            .first()
        )
        if run:
            record_log(run, event[:100], message)


def finish(run_id, token, *, result=None, error=None):
    with transaction.atomic():
        run = (
            Run.objects.select_for_update()
            .filter(pk=run_id, state=State.RUNNING, lease_token=token)
            .first()
        )
        if not run:
            return False  # Fencing: a stale worker cannot overwrite a recovered run.
        now = timezone.now()
        run.move_to(State.FAILED if error is not None else State.SUCCEEDED)
        Attempt.objects.filter(run=run, lease_token=token).update(
            state=run.state, finished_at=now, error=error or ""
        )
        run.lease_token, run.lease_expires_at = None, None
        if error is None:
            run.result, run.last_error = result, ""
            record_log(run, "run.succeeded")
        else:
            run.last_error = error[:4000]
            record_log(run, "run.failed", run.last_error, "warning")
            policy = RetryPolicy.model_validate(run.retry_policy)
            if policy.can_retry(run.cycle_attempt_count):
                run.move_to(State.RETRYING)
                run.next_attempt_at = now + timedelta(seconds=policy.delay(run.cycle_attempt_count))
                Outbox.objects.update_or_create(
                    run=run, defaults={"available_at": run.next_attempt_at, "published_at": None}
                )
                record_log(run, "run.retry_scheduled")
            else:
                run.move_to(State.DEAD)
                record_log(run, "run.dead", run.last_error, "warning")
        run.save()
    return True


def requeue(run_id):
    with transaction.atomic():
        run = Run.objects.select_for_update().get(pk=run_id)
        if run.state != State.DEAD:
            raise Conflict("Only dead-letter runs can be requeued.")
        run.move_to(State.QUEUED)
        run.cycle_attempt_count = 0
        run.next_attempt_at = None
        run.result = None
        run.save()
        Outbox.objects.update_or_create(
            run=run, defaults={"available_at": timezone.now(), "published_at": None}
        )
        record_log(run, "run.requeued")
        transaction.on_commit(lambda: dispatch_one(run.pk), robust=True)
    return run


def maintain():
    expired = list(
        Run.objects.filter(state=State.RUNNING, lease_expires_at__lt=timezone.now()).values_list(
            "id", "lease_token"
        )[:200]
    )
    for run_id, token in expired:
        finish(run_id, token, error="Worker lease expired; execution interrupted or worker lost.")
    ids = list(
        Outbox.objects.filter(available_at__lte=timezone.now())
        .filter(
            Q(published_at__isnull=True)
            | Q(published_at__lte=timezone.now() - timedelta(seconds=env.queue_republish_seconds))
        )
        .order_by("available_at")
        .values_list("run_id", flat=True)[:200]
    )
    for run_id in ids:
        dispatch_one(run_id)
    return {"recovered": len(expired), "examined": len(ids)}
