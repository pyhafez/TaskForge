import json
import os
import queue
import subprocess
import sys
import threading
import time

from structlog.contextvars import bind_contextvars, clear_contextvars

from taskforge.celery import app
from taskforge.config import env
from taskforge.core import services
from taskforge.observability import run_span


def execute_isolated(run):
    child_env = {
        key: value
        for key, value in os.environ.items()
        if key
        in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PYTHONPATH", "VIRTUAL_ENV", "LANG"}
    }
    child_env["HTTP_ALLOWED_HOSTS"] = env.http_allowed_hosts
    request = {
        "task_type": run.task_type,
        "payload": run.payload,
        "context": {
            "run_id": str(run.id),
            "trace_id": run.trace_id,
            "attempt": run.attempt_count,
            "cycle_attempt": run.cycle_attempt_count,
        },
    }
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "taskforge.executor"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        env=child_env,
    )
    events = queue.Queue(maxsize=100)
    stopped = threading.Event()

    def read_output():
        for line in proc.stdout:
            while not stopped.is_set():
                try:
                    events.put(line, timeout=0.1)
                    break
                except queue.Full:
                    continue
            if stopped.is_set():
                return

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    deadline = time.monotonic() + run.timeout_seconds
    result, error, got_result = None, None, False
    count = 0
    try:
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.close()
        while proc.poll() is None or reader.is_alive() or not events.empty():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Task exceeded {run.timeout_seconds} second timeout")
            try:
                event = json.loads(events.get(timeout=0.05))
            except queue.Empty:
                continue
            if event["kind"] == "log":
                count += 1
                if count <= 1000:
                    services.append_output(
                        run.id, run.lease_token, event["event"], event["message"]
                    )
            elif event["kind"] == "result":
                result, got_result = event["result"], True
            elif event["kind"] == "error":
                error = event["message"]
        if error:
            raise ValueError(error)
        if proc.returncode != 0 or not got_result:
            raise ValueError("Task subprocess terminated without a result")
        return result
    finally:
        stopped.set()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        reader.join(timeout=2)
        proc.stdout.close()
        if not proc.stdin.closed:
            proc.stdin.close()


@app.task(name="taskforge.execute_run")
def execute_run(run_id):
    clear_contextvars()
    run = services.claim(run_id)
    if run is None:
        return
    bind_contextvars(run_id=str(run.id), trace_id=run.trace_id)
    try:
        with run_span("run.execute", run):
            try:
                result = execute_isolated(run)
            except Exception as exc:
                error = (
                    str(exc)
                    if isinstance(exc, (ValueError, TimeoutError))
                    else f"Execution failed ({type(exc).__name__})"
                )
                services.finish(run.id, run.lease_token, error=error)
            else:
                services.finish(run.id, run.lease_token, result=result)
    finally:
        clear_contextvars()


@app.task(name="taskforge.maintenance")
def maintenance():
    return services.maintain()
