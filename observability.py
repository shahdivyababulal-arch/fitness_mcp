"""OpenTelemetry bootstrap for the fitness MCP server.

The subset of the fitness agent's observability that a tool server needs.
Everything ADK-specific stays in the agent: this process has no model calls to
instrument and no FastAPI surface, so `instrument_asgi` and the httpx notes
that went with it are gone. What remains is the tool-span decorator and
inbound trace extraction.

Context propagation is inbound-only. `wrap_asgi_app` extracts the W3C
`traceparent` the agent's MCP client injects, so tool spans join the caller's
trace instead of starting their own. Spans are never filtered in-process --
dropping a parent orphans its children, which breaks the cross-service trace.
Filter at the Collector instead.
"""

from __future__ import annotations

import functools
import os
from typing import Any, Callable

from opentelemetry import trace

from config import settings

_tracing_configured = False


def traced_tool(name: str) -> Callable:
    """Create a span for one public fitness MCP business tool."""
    def decorator(function: Callable) -> Callable:
        # functools.wraps sets __wrapped__, so inspect.signature() resolves to
        # the real parameters. Copying only __name__/__doc__ leaves FastMCP
        # introspecting (*args, **kwargs) and publishing a tool schema of
        # {args, kwargs}, which makes every call fail validation.
        @functools.wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with trace.get_tracer("fitness.tool").start_as_current_span(
                f"fitness.tool.{name}",
                attributes={"fitness.tool.name": name},
            ):
                return function(*args, **kwargs)
        return wrapper
    return decorator


def configure_tracing(service_name: str | None = None) -> None:
    """Install the tracer provider for this service."""
    global _tracing_configured
    if _tracing_configured or not settings.otel_enabled:
        return

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resolved = service_name or settings.otel_service_name
    # Resource.create() also merges OTEL_RESOURCE_ATTRIBUTES from the env.
    os.environ.setdefault("OTEL_SERVICE_NAME", resolved)
    # Sampling must be parent-based so the entry agent's decision propagates to
    # every downstream service; independent samplers produce half-traces.
    os.environ.setdefault("OTEL_TRACES_SAMPLER", "parentbased_traceidratio")
    os.environ.setdefault("OTEL_TRACES_SAMPLER_ARG", "1.0")

    # Cloud Run has no Jaeger to export to. Selecting the backend by an
    # explicit env var rather than sniffing for GCP keeps the choice testable
    # locally -- the same reasoning as gs:// config URIs.
    if os.getenv("OTEL_TRACES_EXPORTER", "").lower() == "gcp":
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

        exporter = CloudTraceSpanExporter()
    else:
        endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/")
        exporter = OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")

    provider = TracerProvider(resource=Resource.create({"service.name": resolved}))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _tracing_configured = True


def wrap_asgi_app(app: Any) -> Any:
    """Wrap an already-built ASGI app (FastMCP's) for trace extraction.

    Wraps from the outside instead of using StarletteInstrumentor.
    instrument_app(), which injects into Starlette's middleware stack and
    breaks MCP streamable-HTTP session routing (POST /mcp -> 404). Wrapping
    externally leaves Starlette's own stack untouched.
    """
    if not settings.otel_enabled:
        return app
    from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
    return OpenTelemetryMiddleware(app, exclude_spans=["send", "receive"])
