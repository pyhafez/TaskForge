import re
import uuid
from contextlib import contextmanager

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars, merge_contextvars

from taskforge.config import env


def processors():
    return [
        merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", key="timestamp"),
    ]


def configure_logging():
    structlog.configure(
        processors=[*processors(), structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def json_formatter():
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=processors(),
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    )


_tracing_configured = False


def configure_tracing():
    global _tracing_configured
    if not env.otel_enabled or _tracing_configured:
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.celery import CeleryInstrumentor
    from opentelemetry.instrumentation.django import DjangoInstrumentor
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "taskforge"}))
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=env.otel_exporter_otlp_endpoint.rstrip("/") + "/v1/traces")
        )
    )
    trace.set_tracer_provider(provider)
    DjangoInstrumentor().instrument()
    CeleryInstrumentor().instrument()
    RedisInstrumentor().instrument()
    PsycopgInstrumentor().instrument()
    _tracing_configured = True


def trace_headers():
    if not env.otel_enabled:
        return {}
    from opentelemetry.propagate import inject

    carrier = {}
    inject(carrier)
    return carrier


@contextmanager
def run_span(name, run):
    """Resume the accepted trace across delayed dispatch and worker processes."""
    if not env.otel_enabled:
        yield
        return
    from opentelemetry import trace
    from opentelemetry.propagate import extract

    with trace.get_tracer(__name__).start_as_current_span(
        name,
        context=extract(run.trace_context),
        attributes={"taskforge.run_id": str(run.id)},
    ):
        yield


class RequestContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        clear_contextvars()
        supplied = request.headers.get("X-Trace-ID", "")
        trace_id = supplied if re.fullmatch(r"[a-f0-9]{32}", supplied) else uuid.uuid4().hex
        if env.otel_enabled:
            from opentelemetry import trace

            context = trace.get_current_span().get_span_context()
            if context.is_valid:
                trace_id = format(context.trace_id, "032x")
        request.trace_id = trace_id
        bind_contextvars(trace_id=trace_id)
        try:
            response = self.get_response(request)
            response["X-Trace-ID"] = trace_id
            response["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'"
            )
            structlog.get_logger().info(
                "http.request",
                method=request.method,
                path=request.path,
                status=response.status_code,
            )
            return response
        finally:
            clear_contextvars()
