"""Fitness tool spans must look like travel's.

They diverged: this server emitted `fitness.tool.<name>` carrying only the
tool's name, while travel_mcp emitted `mcp.tool.<name>` carrying the
arguments and the result. In Cloud Trace that meant a travel tool call
showed what it was asked and what it answered, and a fitness one showed
neither -- so the fitness half of a delegation looked absent even though
the tools had run.
"""

import json

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from observability import summarize, traced_tool


# One provider for the whole module: OpenTelemetry ignores a second
# set_tracer_provider call, so a per-test provider would leave every test
# after the first reading an exporter nothing writes to.
_EXPORTER = InMemorySpanExporter()
_PROVIDER = TracerProvider()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))
trace.set_tracer_provider(_PROVIDER)


@pytest.fixture
def spans():
    _EXPORTER.clear()
    yield _EXPORTER


def test_span_is_named_like_travels(spans):
    @traced_tool("log_entry")
    def _tool(**kwargs):
        return {"ok": True}

    _tool(entry_type="meal")
    (span,) = spans.get_finished_spans()
    assert span.name == "mcp.tool.log_entry"


def test_the_arguments_and_result_are_on_the_span(spans):
    """Without these the span says a tool ran but not what it did."""
    @traced_tool("search_food_nutrition")
    def _tool(**kwargs):
        return {"calories_kcal": 562.5}

    _tool(food_query="peanut butter")
    (span,) = spans.get_finished_spans()
    assert span.attributes["mcp.tool.name"] == "search_food_nutrition"
    assert json.loads(span.attributes["mcp.tool.input"]) == {"food_query": "peanut butter"}
    assert json.loads(span.attributes["mcp.tool.output"]) == {"calories_kcal": 562.5}


def test_a_failing_tool_records_the_error_and_reraises(spans):
    @traced_tool("log_entry")
    def _tool(**kwargs):
        raise ValueError("bad entry")

    with pytest.raises(ValueError):
        _tool(entry_type="nonsense")

    (span,) = spans.get_finished_spans()
    assert span.attributes["mcp.tool.error"] == "bad entry"
    assert span.status.status_code.name == "ERROR"


def test_large_payloads_are_bounded():
    """Span attributes are not a data store."""
    big = summarize({"x": "y" * 10_000})
    assert len(big) <= 4000 + len("... [truncated]")
    assert big.endswith("... [truncated]")


def test_the_decorator_still_preserves_the_tool_signature():
    """functools.wraps is load-bearing: without it FastMCP publishes a
    schema of {args, kwargs} and every call fails validation."""
    import inspect

    @traced_tool("calculate_tdee_and_macros")
    def _tool(weight_kg: float, is_male: bool = True):
        return weight_kg

    params = list(inspect.signature(_tool).parameters)
    assert params == ["weight_kg", "is_male"]
