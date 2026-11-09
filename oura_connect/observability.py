"""Structured JSON logging, PII redaction, OpenTelemetry tracing, and Intent-vs-Outcome auditing."""

from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import json
import logging
import logging.handlers
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Iterator

from .config import default_data_dir

# Optional OpenTelemetry SDK integration (with W3C-compatible fallback when running stdlib-only)
try:
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.resources import Resource as OTelResource
    from opentelemetry.sdk.trace import TracerProvider as OTelTracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor as OTelBatchSpanProcessor
    from opentelemetry.trace import Status as OTelStatus, StatusCode as OTelStatusCode

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except Exception:  # pragma: no cover
        OTLPSpanExporter = None  # type: ignore[assignment]

    HAS_OPENTELEMETRY = True
except Exception:  # pragma: no cover
    otel_trace = None  # type: ignore[assignment]
    HAS_OPENTELEMETRY = False

_CURRENT_TRACE_ID: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")
_CURRENT_SPAN_ID: contextvars.ContextVar[str] = contextvars.ContextVar("span_id", default="")

SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "client_secret",
        "api_key",
        "gemini_api_key",
        "authorization",
        "password",
        "secret",
        "token",
        "email",
    }
)

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b")
_GOOGLE_API_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b")
_GITHUB_TOKEN_RE = re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_KV_SECRET_RE = re.compile(
    r"(?i)\b(access_token|refresh_token|client_secret|api_key)\s*([=:])\s*([^\s,&\"']+)"
)


class PIIRedactor:
    """Scrubs PII (emails, phones, SSNs) and credentials (Bearer tokens, API keys, secrets) recursively."""

    @classmethod
    def redact_text(cls, text: str) -> str:
        if not text:
            return text
        scrubbed = _BEARER_RE.sub("Bearer [REDACTED_TOKEN]", text)
        scrubbed = _GOOGLE_API_KEY_RE.sub("[REDACTED_API_KEY]", scrubbed)
        scrubbed = _GITHUB_TOKEN_RE.sub("[REDACTED_GITHUB_TOKEN]", scrubbed)
        scrubbed = _KV_SECRET_RE.sub(r"\1\2[REDACTED_SECRET]", scrubbed)
        scrubbed = _EMAIL_RE.sub("[REDACTED_EMAIL]", scrubbed)
        scrubbed = _SSN_RE.sub("[REDACTED_SSN]", scrubbed)
        scrubbed = _PHONE_RE.sub("[REDACTED_PHONE]", scrubbed)
        return scrubbed

    @classmethod
    def redact(cls, value: Any) -> Any:
        if isinstance(value, str):
            return cls.redact_text(value)
        if isinstance(value, dict):
            cleaned: dict[str, Any] = {}
            for k, v in value.items():
                key_lower = str(k).lower()
                if key_lower in SENSITIVE_KEYS or any(s in key_lower for s in ("secret", "token", "password", "api_key")):
                    cleaned[str(k)] = "[REDACTED_EMAIL]" if key_lower == "email" else "[REDACTED_SECRET]"
                else:
                    cleaned[str(k)] = cls.redact(v)
            return cleaned
        if isinstance(value, (list, tuple)):
            return [cls.redact(item) for item in value]
        return value


class PIIRedactingFilter(logging.Filter):
    """Logging filter guaranteeing no unredacted PII or tokens reach disk or stderr."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = PIIRedactor.redact_text(str(record.msg))
        if record.args:
            record.args = tuple(PIIRedactor.redact(a) for a in record.args)
        extra_data = getattr(record, "structured_data", None)
        if extra_data is not None:
            setattr(record, "structured_data", PIIRedactor.redact(extra_data))
        return True


class JsonLogFormatter(logging.Formatter):
    """Formats Python LogRecords as single-line structured JSON with W3C trace correlation."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event_name", "log"),
            "message": PIIRedactor.redact_text(record.getMessage()),
            "trace_id": getattr(record, "trace_id", None) or _CURRENT_TRACE_ID.get() or None,
            "span_id": getattr(record, "span_id", None) or _CURRENT_SPAN_ID.get() or None,
        }
        structured = getattr(record, "structured_data", None)
        if isinstance(structured, dict) and structured:
            payload["attributes"] = PIIRedactor.redact(structured)
        if record.exc_info:
            payload["exception"] = PIIRedactor.redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, sort_keys=True)


def _resolve_log_dir() -> Path:
    override = os.environ.get("OURA_CONNECT_LOG_DIR")
    base = Path(override).expanduser() if override else (default_data_dir() / "logs")
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    return base


def get_structured_logger(name: str = "jackie_agent") -> logging.Logger:
    """Return a configured JSON logger backed by a PII-redacting rotating JSONL handler."""
    logger = logging.getLogger(name)
    if getattr(logger, "_jackie_configured", False):
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False
    redactor_filter = PIIRedactingFilter()
    formatter = JsonLogFormatter()

    try:
        log_file = _resolve_log_dir() / "agent.jsonl"
        file_handler = logging.handlers.RotatingFileHandler(
            str(log_file), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redactor_filter)
        logger.addHandler(file_handler)
    except OSError:
        pass

    if os.environ.get("JACKIE_AGENT_STDERR_LOGS") == "1":
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        stream_handler.addFilter(redactor_filter)
        logger.addHandler(stream_handler)

    if not logger.handlers:
        null_handler = logging.NullHandler()
        logger.addHandler(null_handler)

    setattr(logger, "_jackie_configured", True)
    return logger


def log_event(
    logger: logging.Logger,
    event: str,
    message: str,
    level: int = logging.INFO,
    **attributes: Any,
) -> dict[str, Any]:
    """Emit a structured JSON log record with automatic PII scrubbing and trace correlation."""
    redacted_attrs = PIIRedactor.redact(attributes)
    logger.log(
        level,
        PIIRedactor.redact_text(message),
        extra={
            "event_name": event,
            "structured_data": redacted_attrs,
            "trace_id": _CURRENT_TRACE_ID.get(),
            "span_id": _CURRENT_SPAN_ID.get(),
        },
    )
    return {
        "event": event,
        "message": PIIRedactor.redact_text(message),
        "trace_id": _CURRENT_TRACE_ID.get(),
        "span_id": _CURRENT_SPAN_ID.get(),
        "attributes": redacted_attrs,
    }


class SpanRecord:
    """W3C TraceContext span supporting OpenTelemetry SDK bridging and local JSONL export."""

    def __init__(
        self,
        name: str,
        trace_id: str,
        span_id: str,
        parent_span_id: str = "",
        attributes: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.start_time_ns = time.time_ns()
        self.end_time_ns: int = 0
        self.status = "OK"
        self.error_message = ""
        self.attributes: dict[str, Any] = PIIRedactor.redact(attributes or {})
        self.events: list[dict[str, Any]] = []

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = PIIRedactor.redact({key: value})[key]

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        self.events.append(
            {
                "name": name,
                "timestamp_ns": time.time_ns(),
                "attributes": PIIRedactor.redact(attributes or {}),
            }
        )

    def record_exception(self, exc: BaseException) -> None:
        self.status = "ERROR"
        self.error_message = PIIRedactor.redact_text(f"{type(exc).__name__}: {exc}")
        self.add_event("exception", {"exception.type": type(exc).__name__, "exception.message": self.error_message})

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-01"

    def to_dict(self) -> dict[str, Any]:
        duration_ms = round((self.end_time_ns - self.start_time_ns) / 1_000_000.0, 3) if self.end_time_ns else 0.0
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id or None,
            "traceparent": self.traceparent,
            "start_time_unix_nano": self.start_time_ns,
            "end_time_unix_nano": self.end_time_ns,
            "duration_ms": duration_ms,
            "status": self.status,
            "error": self.error_message or None,
            "attributes": self.attributes,
            "events": self.events,
        }


class TelemetryTracer:
    """Distributed tracer integrating OpenTelemetry SDK with local W3C span persistence."""

    def __init__(self, service_name: str = "jackie-half-marathon-agent") -> None:
        self.service_name = service_name
        self._completed_spans: list[dict[str, Any]] = []
        self._otel_tracer = None

        if HAS_OPENTELEMETRY and otel_trace is not None:
            provider = OTelTracerProvider(resource=OTelResource.create({"service.name": service_name}))
            otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
            if otlp_endpoint and OTLPSpanExporter is not None:
                provider.add_span_processor(OTelBatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint)))
            otel_trace.set_tracer_provider(provider)
            self._otel_tracer = otel_trace.get_tracer(service_name, "0.3.0")

    @contextlib.contextmanager
    def start_as_current_span(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[SpanRecord]:
        parent_trace = _CURRENT_TRACE_ID.get()
        parent_span = _CURRENT_SPAN_ID.get()
        trace_id = parent_trace or secrets.token_hex(16)
        span_id = secrets.token_hex(8)

        token_t = _CURRENT_TRACE_ID.set(trace_id)
        token_s = _CURRENT_SPAN_ID.set(span_id)
        span = SpanRecord(
            name=name,
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span,
            attributes={"service.name": self.service_name, **(attributes or {})},
        )

        otel_ctx = (
            self._otel_tracer.start_as_current_span(name)
            if self._otel_tracer is not None
            else contextlib.nullcontext()
        )

        try:
            with otel_ctx as sdk_span:
                try:
                    yield span
                except BaseException as exc:
                    span.record_exception(exc)
                    if sdk_span is not None and HAS_OPENTELEMETRY:
                        sdk_span.record_exception(exc)
                        sdk_span.set_status(OTelStatus(OTelStatusCode.ERROR, span.error_message))
                    raise
                finally:
                    span.end_time_ns = time.time_ns()
                    if sdk_span is not None:
                        for k, v in span.attributes.items():
                            if isinstance(v, (str, bool, int, float)):
                                sdk_span.set_attribute(k, v)
                    serialized = span.to_dict()
                    self._completed_spans.append(serialized)
                    if len(self._completed_spans) > 500:
                        self._completed_spans = self._completed_spans[-250:]
                    self._persist_span(serialized)
        finally:
            _CURRENT_TRACE_ID.reset(token_t)
            _CURRENT_SPAN_ID.reset(token_s)

    def get_recent_spans(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self._completed_spans[-limit:])

    def _persist_span(self, span_dict: dict[str, Any]) -> None:
        try:
            spans_path = _resolve_log_dir() / "traces.jsonl"
            with spans_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(span_dict, sort_keys=True) + "\n")
        except OSError:
            pass


class IntentOutcomeTracker:
    """Explicitly pairs declared agent INTENT against actual verified OUTCOME to detect drift."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or get_structured_logger("jackie_agent.intent_outcome")
        self._records: list[dict[str, Any]] = []

    def record_intent(
        self,
        correlation_id: str,
        declared_intent: str,
        selected_agent: str,
        routed_model: str,
        planned_tools: list[str] | None = None,
        constraints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        entry = {
            "correlation_id": correlation_id,
            "phase": "INTENT",
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "trace_id": _CURRENT_TRACE_ID.get() or None,
            "declared_intent": PIIRedactor.redact_text(declared_intent),
            "selected_agent": selected_agent,
            "routed_model": routed_model,
            "planned_tools": list(planned_tools or []),
            "constraints": PIIRedactor.redact(constraints or {}),
        }
        log_event(self.logger, "agent.intent", f"Declared intent: {declared_intent}", **entry)
        return entry

    def record_outcome(
        self,
        correlation_id: str,
        declared_intent: str,
        actual_outcome: str,
        status: str,
        executed_tools: list[str] | None = None,
        guardrail_passed: bool = True,
        hitl_status: str = "NOT_REQUIRED",
        latency_ms: float = 0.0,
        intent_satisfied: bool = True,
        drift_reason: str = "",
    ) -> dict[str, Any]:
        entry = {
            "correlation_id": correlation_id,
            "phase": "OUTCOME",
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "trace_id": _CURRENT_TRACE_ID.get() or None,
            "declared_intent": PIIRedactor.redact_text(declared_intent),
            "actual_outcome": PIIRedactor.redact_text(actual_outcome),
            "status": status,
            "executed_tools": list(executed_tools or []),
            "guardrail_passed": guardrail_passed,
            "hitl_status": hitl_status,
            "intent_satisfied": intent_satisfied,
            "drift_detected": not intent_satisfied,
            "drift_reason": PIIRedactor.redact_text(drift_reason),
            "latency_ms": round(latency_ms, 2),
        }
        self._records.append(entry)
        if len(self._records) > 200:
            self._records = self._records[-100:]
        log_event(self.logger, "agent.outcome", f"Outcome ({status}): {actual_outcome}", **entry)
        return entry

    def get_recent_outcomes(self, limit: int = 25) -> list[dict[str, Any]]:
        return list(self._records[-limit:])


DEFAULT_TRACER = TelemetryTracer()
DEFAULT_LOGGER = get_structured_logger()
DEFAULT_INTENT_TRACKER = IntentOutcomeTracker(DEFAULT_LOGGER)
