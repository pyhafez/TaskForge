"""Trusted task plug-ins. No user-supplied Python or shell commands are executed."""

import ipaddress
import os
import socket
import time
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

TASK_TYPES = {}


def register(name, schema):
    def decorator(handler):
        if name in TASK_TYPES:
            raise ValueError(f"Task type {name!r} is already registered")
        TASK_TYPES[name] = (schema, handler)
        return handler

    return decorator


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SleepPayload(Payload):
    seconds: float = Field(default=1, ge=0, le=3600)


@register("sleep", SleepPayload)
def sleep_task(payload, context, emit):
    deadline = time.monotonic() + payload.seconds
    emit("sleep.started", f"Sleeping for {payload.seconds:g} seconds")
    while (remaining := deadline - time.monotonic()) > 0:
        time.sleep(min(1, remaining))
        emit("sleep.progress", f"{max(0, deadline - time.monotonic()):.1f} seconds remaining")
    return {"slept": payload.seconds}


class DemoPayload(Payload):
    value: str = Field(default="hello", max_length=4000)
    fail_until: int = Field(default=0, ge=0, le=20)


@register("python_demo", DemoPayload)
def demo_task(payload, context, emit):
    emit("demo.output", payload.value)
    if context["cycle_attempt"] <= payload.fail_until:
        raise ValueError("Injected transient demo failure")
    return {"value": payload.value, "attempt": context["attempt"]}


class HTTPPayload(Payload):
    url: str = Field(max_length=2048)


@register("http_call", HTTPPayload)
def http_task(payload, context, emit):
    parsed = urlsplit(payload.url)
    allowed = {
        host.strip().lower()
        for host in os.environ.get("HTTP_ALLOWED_HOSTS", "").split(",")
        if host.strip()
    }
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError("HTTP tasks require an allowlisted HTTPS host on port 443")
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("HTTP destination must resolve to public addresses")
    emit("http.started", f"GET https://{parsed.hostname}")
    # Streaming caps memory; redirects and ambient proxy configuration are disabled.
    with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
        with client.stream(
            "GET",
            payload.url,
            headers={"Idempotency-Key": context["run_id"], "X-Trace-ID": context["trace_id"]},
        ) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > 32_768:
                    raise ValueError("HTTP response exceeds 32 KiB")
            return {
                "status_code": response.status_code,
                "body": data.decode("utf-8", errors="replace"),
            }


def validate_task_payload(task_type, payload):
    if task_type not in TASK_TYPES:
        raise ValueError("Unknown task type")
    return TASK_TYPES[task_type][0].model_validate(payload).model_dump()
