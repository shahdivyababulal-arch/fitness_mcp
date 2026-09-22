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

Cloud Trace is the only export backend. Set `OTEL_ENABLED=false` to run with
tracing off; there is no second backend to select between.
"""

from __future__ import annotations

import functools
import json
import logging
import os
from typing import Any, Callable

from opentelemetry import trace

from config import settings

_tracing_configured = False

logger = logging.getLogger("fitness.tool")

_SPAN_ATTRIBUTE_LIMIT = 4000


def summarize(value: Any, limit: int = _SPAN_ATTRIBUTE_LIMIT) -> str:
    """Serialize tool data without allowing large payloads into telemetry.

    Bounded because span attributes are not a data store: an unbounded tool
    result bloats every trace and can be dropped by the exporter.
    """
    serialized = json.dumps(value, default=str, sort_keys=True)
    if len(serialized) <= limit:
        return serialized
    return serialized[:limit] + "... [truncated]"


def traced_tool(name: str) -> Callable:
    """Create a span for one public fitness MCP business tool.

    Deliberately the same span name, attribute names and failure handling as
    travel_mcp's `_logged`. They diverged before: this emitted
    `fitness.tool.<name>` carrying only the tool's name, while travel emitted
    `mcp.tool.<name>` carrying the arguments and the result. The effect in
    Cloud Trace was that a travel tool call showed what it was asked and what
    it answered, and a fitness one showed neither -- so the fitness half of a
    delegation looked absent even though the tools had run.

    Filter both with `mcp.tool.` now; the service name still tells them apart.
    """
    def decorator(function: Callable) -> Callable:
        # functools.wraps sets __wrapped__, so inspect.signature() resolves to
        # the real parameters. Copying only __name__/__doc__ leaves FastMCP
        # introspecting (*args, **kwargs) and publishing a tool schema of
        # {args, kwargs}, which makes every call fail validation.
        @functools.wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with trace.get_tracer("fitness.mcp").start_as_current_span(
                f"mcp.tool.{name}",
                attributes={
                    "mcp.tool.name": name,
                    # positional args are unnamed here; the tools are called
                    # by keyword through FastMCP, so kwargs is the real input
                    "mcp.tool.input": summarize(kwargs),
                },
            ) as span:
                try:
                    result = function(*args, **kwargs)
                    span.set_attribute("mcp.tool.output", summarize(result))
                    logger.info("mcp_tool_execution_completed tool_name=%s status=success", name)
                    return result
                except Exception as error:
                    span.record_exception(error)
                    span.set_status(trace.StatusCode.ERROR, str(error))
                    span.set_attribute("mcp.tool.error", str(error))
                    logger.info("mcp_tool_execution_completed tool_name=%s status=failure "
                                "error_type=%s", name, type(error).__name__)
                    raise
        return wrapper
    return decorator


def configure_tracing(service_name: str | None = None) -> None:
    """Export this server's spans to Cloud Trace.

    Requires application default credentials: in Cloud Run that is the
    runtime service account, and locally `gcloud auth application-default
    login`. Without them, run with `OTEL_ENABLED=false`.
    """
    global _tracing_configured
    if _tracing_configured or not settings.otel_enabled:
        return

    from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
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

    provider = TracerProvider(resource=Resource.create({"service.name": resolved}))
    provider.add_span_processor(BatchSpanProcessor(CloudTraceSpanExporter()))
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
