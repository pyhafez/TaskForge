import json
import logging
from types import SimpleNamespace

import pytest
from structlog.contextvars import bound_contextvars

from taskforge.config import env
from taskforge.observability import json_formatter, run_span, trace_headers


def test_stdlib_logs_have_json_fields_and_correlation():
    record = logging.LogRecord("taskforge", logging.INFO, __file__, 1, "event.name", (), None)
    with bound_contextvars(trace_id="a" * 32, run_id="run-1"):
        data = json.loads(json_formatter().format(record))
    assert data["event"] == "event.name"
    assert data["level"] == "info"
    assert data["timestamp"]
    assert data["trace_id"] == "a" * 32
    assert data["run_id"] == "run-1"


def test_delayed_publish_and_execution_resume_original_trace(monkeypatch):
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    monkeypatch.setattr(env, "otel_enabled", True)
    monkeypatch.setattr(trace, "get_tracer", lambda *args, **kwargs: tracer)
    with tracer.start_as_current_span("request") as parent:
        run = SimpleNamespace(id="run-1", trace_context=trace_headers())
        original = parent.get_span_context().trace_id
    # There is no active request span by the time the dispatcher sees the outbox.
    with run_span("run.publish", run):
        pass
    with run_span("run.execute", run):
        pass
    spans = exporter.get_finished_spans()
    assert {span.context.trace_id for span in spans} == {original}
    assert {span.name for span in spans} == {"request", "run.publish", "run.execute"}
    provider.shutdown()
