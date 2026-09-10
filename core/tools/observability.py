"""Phase 4.2 - Observability integration (Prometheus / Loki / Tempo).

Read-only HTTP query tools + SLO checker. Base URLs come from config["observability"];
unconfigured clients no-op with a clear ToolResult(False) so the agent never crashes.

Scenario hook: query_logs finds root cause -> edit_file/apply_patch_set hotfix ->
kubectl rollout restart (infra tools) -> query_metrics confirms recovery.
"""
from typing import Dict, List, Optional
from core.tools.base import BaseTool, ToolResult


def _http_get(url: str, params: Dict, timeout: float):
    import httpx
    r = httpx.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


class _ObsClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout

    def enabled(self) -> bool:
        return bool(self.base_url)


class MetricsClient(_ObsClient):
    def query(self, promql: str) -> Dict:
        return _http_get(f"{self.base_url}/api/v1/query", {"query": promql}, self.timeout)


class LogsClient(_ObsClient):
    def query_range(self, logql: str, start: Optional[str] = None, end: Optional[str] = None, limit: int = 100) -> Dict:
        p = {"query": logql, "limit": str(limit)}
        if start: p["start"] = start
        if end: p["end"] = end
        return _http_get(f"{self.base_url}/loki/api/v1/query_range", p, self.timeout)


class TracesClient(_ObsClient):
    def search(self, tags: str, limit: int = 20) -> Dict:
        return _http_get(f"{self.base_url}/api/search", {"tags": tags, "limit": str(limit)}, self.timeout)


class QueryMetricsTool(BaseTool):
    name = "query_metrics"
    description = "Run a PromQL instant query against Prometheus and return the raw vector. Use to check SLOs (error rate, p99 latency) before/after a change or rollout."
    parameters = {"type": "object", "properties": {
        "promql": {"type": "string", "description": "PromQL expr, e.g. sum(rate(http_requests_total{code=~'5..'}[1m])) / sum(rate(http_requests_total[1m]))"},
        "thought": {"type": "string", "description": "Why you are querying metrics now"}},
        "required": ["promql", "thought"]}

    def __init__(self, client: MetricsClient):
        self.client = client

    def execute(self, promql: str, thought: str = "") -> ToolResult:
        if not self.client.enabled():
            return ToolResult(False, error="observability.prometheus_url not configured")
        try:
            data = self.client.query(promql)
            n = len(data.get("data", {}).get("result", []))
            return ToolResult(True, data=data, summary=f"query_metrics: {n} series")
        except Exception as e:
            return ToolResult(False, error=f"query_metrics failed: {e}")


class QueryLogsTool(BaseTool):
    name = "query_logs"
    description = "Run a LogQL range query against Loki. Use to find the root-cause error/stacktrace behind a metric regression."
    parameters = {"type": "object", "properties": {
        "logql": {"type": "string", "description": "LogQL expr, e.g. {app='api'} |= 'error'"},
        "limit": {"type": "integer", "default": 100},
        "thought": {"type": "string"}},
        "required": ["logql", "thought"]}

    def __init__(self, client: LogsClient):
        self.client = client

    def execute(self, logql: str, thought: str = "", limit: int = 100) -> ToolResult:
        if not self.client.enabled():
            return ToolResult(False, error="observability.loki_url not configured")
        try:
            data = self.client.query_range(logql, limit=limit)
            streams = data.get("data", {}).get("result", [])
            lines = sum(len(s.get("values", [])) for s in streams)
            return ToolResult(True, data=data, summary=f"query_logs: {lines} line(s) across {len(streams)} stream(s)")
        except Exception as e:
            return ToolResult(False, error=f"query_logs failed: {e}")


class QueryTracesTool(BaseTool):
    name = "query_traces"
    description = "Search traces in Tempo by tag selector. Use to locate the slow/failing span for a latency regression."
    parameters = {"type": "object", "properties": {
        "traceql": {"type": "string", "description": "Tempo tag selector, e.g. http.status_code>=500"},
        "thought": {"type": "string"}},
        "required": ["traceql", "thought"]}

    def __init__(self, client: TracesClient):
        self.client = client

    def execute(self, traceql: str, thought: str = "") -> ToolResult:
        if not self.client.enabled():
            return ToolResult(False, error="observability.tempo_url not configured")
        try:
            data = self.client.search(traceql)
            tr = data.get("traces", [])
            return ToolResult(True, data=data, summary=f"query_traces: {len(tr)} trace(s)")
        except Exception as e:
            return ToolResult(False, error=f"query_traces failed: {e}")


def _last_scalar(prom_data: Dict) -> Optional[float]:
    for r in prom_data.get("data", {}).get("result", []):
        try:
            return float(r["value"][1])
        except Exception:
            continue
    return None


class SLOChecker:
    """Evaluate SLO thresholds from PromQL expressions. `pass` iff every metric is within bound."""

    def __init__(self, metrics_client: MetricsClient, slo_cfg: Dict):
        self.client = metrics_client
        self.rules = (slo_cfg or {}).get("rules", [])  # [{name, promql, max?, min?}]

    def check(self) -> ToolResult:
        if not self.client.enabled():
            return ToolResult(False, error="SLO check needs prometheus_url")
        detail, ok = [], True
        for rule in self.rules:
            try:
                val = _last_scalar(self.client.query(rule["promql"]))
            except Exception as e:
                detail.append({"name": rule.get("name"), "error": str(e)}); ok = False; continue
            entry = {"name": rule.get("name"), "value": val, "max": rule.get("max"), "min": rule.get("min")}
            if val is None:
                entry["pass"] = False; ok = False
            else:
                p = True
                if rule.get("max") is not None and val > rule["max"]: p = False
                if rule.get("min") is not None and val < rule["min"]: p = False
                entry["pass"] = p; ok = ok and p
            detail.append(entry)
        return ToolResult(ok, data={"pass": ok, "rules": detail},
                          summary=f"SLO {'PASS' if ok else 'BREACH'}: " + ", ".join(
                              f"{d.get('name')}={d.get('value')}" for d in detail))


def build_observability_tools(config: Dict) -> List[BaseTool]:
    obs = (config or {}).get("observability", {}) or {}
    mc, lc, tc = MetricsClient(obs.get("prometheus_url", "")), LogsClient(obs.get("loki_url", "")), TracesClient(obs.get("tempo_url", ""))
    tools: List[BaseTool] = [QueryMetricsTool(mc), QueryLogsTool(lc), QueryTracesTool(tc)]
    return tools, mc
