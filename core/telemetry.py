"""
core/telemetry.py — OpenTelemetry + Structured Logging for JARVIS.

One-time initialization in main.py before any agent/provider loads.
All spans/logs carry: conversation_id, turn_id, agent_name, provider, model.
"""
from __future__ import annotations

import os
import sys
import uuid
from contextvars import ContextVar
from typing import Any

import structlog
from opentelemetry import trace, metrics, baggage
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader, ConsoleMetricExporter
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.asyncio import AsyncioInstrumentor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.b3 import B3MultiFormat
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

# ── Context propagation (conversation/turn IDs flow through async boundaries) ──
_conversation_id: ContextVar[str | None] = ContextVar("conversation_id", default=None)
_turn_id: ContextVar[str | None] = ContextVar("turn_id", default=None)
_agent_name: ContextVar[str | None] = ContextVar("agent_name", default=None)

def set_conversation_id(cid: str | None) -> None:
    _conversation_id.set(cid)

def get_conversation_id() -> str | None:
    return _conversation_id.get()

def set_turn_id(tid: str | None) -> None:
    _turn_id.set(tid)

def get_turn_id() -> str | None:
    return _turn_id.get()

def set_agent_name(name: str | None) -> None:
    _agent_name.set(name)

def get_agent_name() -> str | None:
    return _agent_name.get()


# ── Structured logging (JSON, correlated with trace IDs) ─────────────────────
def _add_trace_context(logger, method_name, event_dict):
    """Inject current span context into every log line."""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.trace_id:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    # Also pull our custom context vars
    for key, getter in (("conversation_id", get_conversation_id),
                         ("turn_id", get_turn_id),
                         ("agent_name", get_agent_name)):
        val = getter()
        if val:
            event_dict[key] = val
    return event_dict


def configure_logging(level: str = "INFO") -> structlog.BoundLogger:
    """Call once at process startup. Returns a ready-to-use logger."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_trace_context,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    return structlog.get_logger("jarvis")


# ── OpenTelemetry setup ──────────────────────────────────────────────────────
_initialized = False

def init_telemetry(
    service_name: str = "jarvis",
    version: str = "0.0.0",
    otlp_endpoint: str | None = None,  # e.g. "http://localhost:4317"
    enable_console: bool = True,
) -> tuple[trace.Tracer, metrics.Meter]:
    """
    Initialize tracing + metrics. Idempotent.
    Returns (tracer, meter) for manual instrumentation.
    """
    global _initialized
    if _initialized:
        return trace.get_tracer(service_name), metrics.get_meter(service_name)

    resource = Resource.create({
        SERVICE_NAME: service_name,
        SERVICE_VERSION: version,
        "deployment.environment": os.getenv("JARVIS_ENV", "development"),
    })

    # Tracer
    provider = TracerProvider(resource=resource)
    if otlp_endpoint:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint)))
    if enable_console:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)

    # Meter
    readers = []
    if otlp_endpoint:
        readers.append(PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=otlp_endpoint)))
    if enable_console:
        readers.append(PeriodicExportingMetricReader(ConsoleMetricExporter()))
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))

    # Propagators (W3C tracecontext + B3 for compatibility)
    set_global_textmap(TraceContextTextMapPropagator())
    # B3 for legacy dashboards
    # set_global_textmap(B3MultiFormat())  # uncomment if needed

    # Auto-instrument common libraries
    RequestsInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    AsyncioInstrumentor().instrument()

    _initialized = True
    return trace.get_tracer(service_name), metrics.get_meter(service_name)


# ── Convenience: common meters/counters ──────────────────────────────────────
def get_meter(name: str = "jarvis") -> metrics.Meter:
    return metrics.get_meter(name)


# Pre-built instruments (lazy init via get_meter)
_llm_calls_total = None
_llm_latency = None
_llm_tokens = None
_agent_calls_total = None
_agent_latency = None
_tool_calls_total = None
_ws_reconnects = None

def _ensure_instruments():
    global _llm_calls_total, _llm_latency, _llm_tokens
    global _agent_calls_total, _agent_latency, _tool_calls_total, _ws_reconnects
    if _llm_calls_total is not None:
        return
    m = get_meter()
    _llm_calls_total = m.create_counter(
        "jarvis.llm.calls.total",
        description="Total LLM provider calls",
        unit="1",
    )
    _llm_latency = m.create_histogram(
        "jarvis.llm.latency",
        description="LLM call latency",
        unit="s",
    )
    _llm_tokens = m.create_histogram(
        "jarvis.llm.tokens",
        description="Tokens used per call (in/out/total)",
        unit="1",
    )
    _agent_calls_total = m.create_counter(
        "jarvis.agent.calls.total",
        description="Agent handle() invocations",
        unit="1",
    )
    _agent_latency = m.create_histogram(
        "jarvis.agent.latency",
        description="Agent handle() latency",
        unit="s",
    )
    _tool_calls_total = m.create_counter(
        "jarvis.tool.calls.total",
        description="Tool invocations",
        unit="1",
    )
    _ws_reconnects = m.create_counter(
        "jarvis.websocket.reconnects",
        description="Live API WebSocket reconnects",
        unit="1",
    )


# ── Helper decorators/context managers ───────────────────────────────────────
from contextlib import contextmanager
import time

@contextmanager
def trace_llm_call(provider: str, model: str, task: str = "general"):
    """Wrap an LLM provider call. Records latency, tokens, success/failure."""
    _ensure_instruments()
    tracer = trace.get_tracer("jarvis.llm")
    with tracer.start_as_current_span(f"llm.{provider}") as span:
        span.set_attribute("llm.provider", provider)
        span.set_attribute("llm.model", model)
        span.set_attribute("llm.task", task)
        start = time.perf_counter()
        error = None
        try:
            yield span
        except Exception as e:
            error = e
            span.set_attribute("error", True)
            span.set_attribute("error.type", type(e).__name__)
            span.set_attribute("error.message", str(e)[:500])
            raise
        finally:
            elapsed = time.perf_counter() - start
            _llm_latency.record(elapsed, attributes={"provider": provider, "model": model})
            _llm_calls_total.add(1, attributes={
                "provider": provider, "model": model, "task": task,
                "status": "error" if error else "ok"
            })


@contextmanager
def trace_agent_call(agent_name: str, task_type: str = ""):
    """Wrap an agent's handle() call."""
    _ensure_instruments()
    tracer = trace.get_tracer("jarvis.agent")
    set_agent_name(agent_name)
    with tracer.start_as_current_span(f"agent.{agent_name}") as span:
        span.set_attribute("agent.name", agent_name)
        if task_type:
            span.set_attribute("agent.task_type", task_type)
        start = time.perf_counter()
        error = None
        try:
            yield span
        except Exception as e:
            error = e
            span.set_attribute("error", True)
            span.set_attribute("error.type", type(e).__name__)
            raise
        finally:
            elapsed = time.perf_counter() - start
            _agent_latency.record(elapsed, attributes={"agent": agent_name})
            _agent_calls_total.add(1, attributes={
                "agent": agent_name, "task_type": task_type,
                "status": "error" if error else "ok"
            })


@contextmanager
def trace_tool_call(tool_name: str, agent: str = ""):
    """Wrap a tool invocation."""
    _ensure_instruments()
    tracer = trace.get_tracer("jarvis.tool")
    with tracer.start_as_current_span(f"tool.{tool_name}") as span:
        span.set_attribute("tool.name", tool_name)
        if agent:
            span.set_attribute("tool.agent", agent)
        start = time.perf_counter()
        error = None
        try:
            yield span
        except Exception as e:
            error = e
            span.set_attribute("error", True)
            span.set_attribute("error.type", type(e).__name__)
            raise
        finally:
            _tool_calls_total.add(1, attributes={
                "tool": tool_name, "agent": agent,
                "status": "error" if error else "ok"
            })


def record_llm_tokens(provider: str, model: str, prompt_tokens: int, completion_tokens: int):
    _ensure_instruments()
    _llm_tokens.record(prompt_tokens, attributes={"provider": provider, "model": model, "type": "prompt"})
    _llm_tokens.record(completion_tokens, attributes={"provider": provider, "model": model, "type": "completion"})
    _llm_tokens.record(prompt_tokens + completion_tokens, attributes={"provider": provider, "model": model, "type": "total"})


def record_ws_reconnect(reason: str = "unknown"):
    _ensure_instruments()
    _ws_reconnects.add(1, attributes={"reason": reason})


# ── FastAPI integration helper ───────────────────────────────────────────────
def instrument_fastapi(app):
    """Call from dashboard/server.py after creating the FastAPI app."""
    FastAPIInstrumentor.instrument_app(app)


# ── Quick health check ───────────────────────────────────────────────────────
def is_initialized() -> bool:
    return _initialized
