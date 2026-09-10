"""Phase 14.1 - OpenTelemetry-native tracing of the whole run.

One trace per agent run: llm.chat / tool.* / a2a.<persona> / patch become spans. Two
exporters, independently enabled:
  * OTLP gRPC/http        - when OTEL_EXPORTER_OTLP_ENDPOINT is reachable (Grafana Tempo,
                            Jaeger collector ...); auto-detected, silently off otherwise.
  * JSONL file (always)   - .cortex_memory/traces/<run_id>.spans.jsonl - the sandbox/offline
                            twin of a collector; importable, greppable, dashboard-friendly.

If opentelemetry is not installed, every API here degrades to a no-op span (never breaks
a run): `tracing_available()` tells you which mode you're in.
"""
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Optional

try:
    from opentelemetry import trace as _t
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (BatchSpanProcessor, SimpleSpanProcessor,
                                                 SpanExporter, SpanExportResult)
    HAVE_OTEL = True
except Exception:  # pragma: no cover
    HAVE_OTEL = False


def tracing_available() -> bool:
    return HAVE_OTEL


class JsonlSpanExporter(SpanExporter if HAVE_OTEL else object):
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = self.path.open("a", encoding="utf-8")

    def export(self, spans):
        for sp in spans:
            ctx = sp.get_span_context()
            rec = {"name": sp.name, "trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x"),
                   "parent": (format(sp.parent.span_id, "016x") if getattr(sp, "parent", None) else None),
                   "start_ns": sp.start_time, "end_ns": sp.end_time,
                   "duration_ms": round((sp.end_time - sp.start_time) / 1e6, 2),
                   "status": str(getattr(sp.status, "status_code", "")),
                   "attributes": {k: (v if isinstance(v, (int, float, bool, str)) else str(v))
                                  for k, v in (sp.attributes or {}).items()}}
            self.f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.f.flush()
        return SpanExportResult.SUCCESS

    def shutdown(self):
        try:
            self.f.close()
        except Exception:
            pass


class _NoopSpan:
    def set_attribute(self, *a, **k): pass
    def set_status(self, *a, **k): pass
    def record_exception(self, *a, **k): pass


class _NoopTracer:
    @contextmanager
    def start_as_current_span(self, *a, **k):
        yield _NoopSpan()


_TRACER = None
_INIT_DONE = False


def init_tracing(service_name: str = "cortex-agent", traces_dir: Optional[Path] = None) -> bool:
    """idempotent; returns True when real spans flow."""
    global _TRACER, _INIT_DONE
    if _INIT_DONE:
        return _TRACER is not _NOOP and _TRACER is not None
    _INIT_DONE = True
    if not HAVE_OTEL:
        _TRACER = _NoopTracer()
        return False
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if traces_dir:
        provider.add_span_processor(SimpleSpanProcessor(JsonlSpanExporter(Path(traces_dir) / f"run_{int(time.time())}.spans.jsonl")))
    ep = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if ep:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=ep + "/v1/traces")))
        except Exception:
            pass
    _t.set_tracer_provider(provider)
    _TRACER = _t.get_tracer(service_name)
    return True


def _tracer():
    return _TRACER or _NoopTracer()


@contextmanager
def span(name: str, **attrs):
    with _tracer().start_as_current_span(name) as sp:
        for k, v in attrs.items():
            try:
                sp.set_attribute(k, v if isinstance(v, (int, float, bool)) else str(v)[:500])
            except Exception:
                pass
        try:
            yield sp
        except Exception as e:
            try:
                sp.record_exception(e)
                from opentelemetry.trace import Status, StatusCode
                sp.set_status(Status(StatusCode.ERROR, str(e)[:200]))
            except Exception:
                pass
            raise


def trace_tool_call(tool_name: str, args: dict):
    return span(f"tool.{tool_name}", **{"tool.name": tool_name, "tool.args": str(args)[:1000]})
