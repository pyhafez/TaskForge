"""Child-process protocol: bounded NDJSON events on stdout, JSON request on stdin."""

import json
import os
import signal
import sys

from taskforge.task_types import TASK_TYPES


def send(kind, **data):
    print(json.dumps({"kind": kind, **data}, allow_nan=False), flush=True)


def main():
    # Linux workers kill their task child if the worker itself is forcibly killed.
    if sys.platform == "linux":
        import ctypes

        parent = os.getppid()
        ctypes.CDLL(None).prctl(1, signal.SIGKILL)
        if parent == 1 or os.getppid() != parent:
            return 1
    request = json.loads(sys.stdin.readline())
    try:
        schema, handler = TASK_TYPES[request["task_type"]]
        payload = schema.model_validate(request["payload"])
        result = handler(
            payload,
            request["context"],
            lambda event, message: send("log", event=event, message=message),
        )
        if len(json.dumps(result, allow_nan=False).encode()) > 65_536:
            raise ValueError("Task result exceeds 64 KiB")
        send("result", result=result)
        return 0
    except Exception as exc:
        # Task failures are data; no tracebacks or environment details cross the protocol.
        message = (
            str(exc)[:2000]
            if isinstance(exc, (ValueError, TimeoutError))
            else f"Task failed ({type(exc).__name__})"
        )
        send("error", message=message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
