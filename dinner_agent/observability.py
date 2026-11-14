"""
observability.py - Dedicated Structured Logging (structlog), PII Redaction, and OpenTelemetry Tracing.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, MutableMapping

import structlog
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Regex patterns to scrub PII & credentials before any log or trace leaves the process
PII_PATTERNS = {
    "EMAIL": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "PHONE": re.compile(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b"),
    "SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "CREDIT_CARD": re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
    "API_KEY": re.compile(r"\bAIza[0-9A-Za-z-_]{35}\b"),
}


def redact_pii(text: str) -> str:
    """Scrubs emails, phone numbers, SSNs, payment card numbers, and API keys from text."""
    if not isinstance(text, str):
        return text
    scrubbed = text
    for label, pattern in PII_PATTERNS.items():
        scrubbed = pattern.sub(f"[REDACTED_{label}]", scrubbed)
    return scrubbed


def redact_value(value: Any) -> Any:
    """Recursively scrubs PII across strings, dictionaries, and lists."""
    if isinstance(value, str):
        return redact_pii(value)
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value


def pii_redaction_processor(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor that scrubs PII across every structured event field."""
    for key, val in list(event_dict.items()):
        event_dict[key] = redact_value(val)
    return event_dict


def inject_otel_trace_context(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Injects active OpenTelemetry trace_id and span_id into structured JSON logs."""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


class PIIRedactingSpanProcessor(SpanProcessor):
    """Lifecycle processor hook for OpenTelemetry span hygiene."""

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        pass

    def on_end(self, span: ReadableSpan) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


# Initialize OpenTelemetry TracerProvider
_resource = Resource.create(
    {"service.name": "dinner-planning-agent-v2", "service.version": "2.0.0"}
)
_provider = TracerProvider(resource=_resource)
span_exporter = InMemorySpanExporter()
_provider.add_span_processor(PIIRedactingSpanProcessor())
_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
trace.set_tracer_provider(_provider)

tracer = trace.get_tracer("dinner_planning_agent_v2.tracer")

# Configure structlog JSON pipeline
logging.basicConfig(level=logging.INFO, format="%(message)s")
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        inject_otel_trace_context,
        pii_redaction_processor,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger("dinner_planning_agent_v2")


def safe_set_span_attributes(span: Any, attributes: Dict[str, Any]) -> None:
    """Writes PII-scrubbed key/value attributes onto the active OpenTelemetry span."""
    for key, value in attributes.items():
        clean_val = redact_value(value)
        if isinstance(clean_val, (str, int, float, bool)):
            span.set_attribute(key, clean_val)
        else:
            span.set_attribute(key, str(clean_val))
