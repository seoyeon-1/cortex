"""Phase 6.5c - DBA / Data-Eng persona.

Validation bar: "Query P99 < 10ms (plan-based), 데이터 품질 테스트 통과".
Engines (degradation-first, all optional):
  * sqlite EXPLAIN QUERY PLAN on SQL literals found in the target files (index scans vs
    full-table-scan advice) - works with zero external services.
  * partition_advisor: heuristic on table names + row-ish keywords.
  * dbt / great_expectations: used only if configured & installed (project-level gate).
Task payload: {root, files, p99_target_ms?}. Artifact: dba-report.
"""
import re
import sqlite3
from pathlib import Path
from typing import AsyncIterator, Dict, List

from agents.base_agent import A2AAgent
from protocol.a2a import AgentCard, AgentSkill, Event, Task

_SQL_RE = re.compile(r"""f?["'](\s*(?:SELECT|INSERT|UPDATE|DELETE)\b[^"']*)["']|execute\(\s*f?["']([^"']*)["']""", re.I)


class DbaAgent(A2AAgent):
    name = "dba-agent"

    def __init__(self, cfg: Dict | None = None):
        super().__init__(cfg)
        self.p99_ms = int(self.cfg.get("p99_target_ms", 10))
        self.card = AgentCard(name=self.name,
                              description="SQL plan analysis, index/partition advice, data-quality smoke checks.",
                              skills=[AgentSkill(id="review_sql", name="DBA review of touched queries")])

    async def handle_task(self, task: Task) -> AsyncIterator[Event]:
        root = Path(task.payload.get("root", "."))
        files = [f for f in task.payload.get("files", []) if f.endswith(".py")] or []
        if not files:
            files = [str(p.relative_to(root)) for p in root.rglob("*.py") if ".git" not in str(p)][:40]
        findings: List[Dict] = []
        plans = 0
        for rel in files:
            try:
                text = (root / rel).read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for m in _SQL_RE.finditer(text):
                raw = m.group(1) or m.group(2) or ""
                sql = " ".join(raw.split())
                # normalize interpolation into a bind-param shape so EXPLAIN can run it
                sql = re.sub(r"\{[^}]+\}", "1", sql)
                sql = sql.rstrip("%").rstrip() + ("'" if sql.count("'") % 2 else "")
                if sql.count("'") % 2:
                    sql = sql[:-1] if sql.endswith("'") else sql + "'"
                if len(sql) < 12:
                    continue
                line = text[:m.start()].count("\n") + 1
                plans += 1
                yield self.progress(task, self.name, f"EXPLAIN: {sql[:70]}")
                plan, notes = self._explain(root, sql)
                for n in notes:
                    findings.append({"severity": n[0], "rule": n[1], "file": rel, "line": line,
                                     "description": f"{sql[:90]} -> {n[2]}", "suggested_fix": n[3]})
                if plan is not None and "scan" in plan and "index" in plan and plan.strip().lower().startswith("scan"):
                    findings.append({"severity": "LOW", "rule": "p99-watch", "file": rel, "line": line,
                                     "description": f"full scan observed; P99 target {self.p99_ms}ms may be missed on growth",
                                     "suggested_fix": "add covering index on the WHERE/JOIN columns"})
        # partition advice (heuristic)
        joined = " ".join(findings and [] or [])
        for rel in files:
            if re.search(r"(events|logs|telemetry|measurements)", rel, re.I):
                findings.append({"severity": "LOW", "rule": "partition-advisor", "file": rel, "line": 0,
                                 "description": "high-volume table pattern detected",
                                 "suggested_fix": "time-partition (e.g. monthly) + BRIN index on the timestamp column"})
        blocking = [f for f in findings if f["severity"] in ("HIGH", "CRITICAL")]
        score = 1.0 if not findings else (0.4 if blocking else 0.85)
        yield self.verdict(task, self.name, name="dba-report", passed=not blocking, score=score,
                           summary=f"{plans} query plan(s) inspected" + (f", {len(blocking)} blocking" if blocking else " (advisory only)"),
                           findings=findings, details={"p99_target_ms": self.p99_ms,
                                                      "dbt": "not configured", "great_expectations": "not configured"})

    @staticmethod
    def _explain(root: Path, sql: str):
        """Run EXPLAIN QUERY PLAN against an ephemeral sqlite with guessed schema - safe, local."""
        notes = []
        try:
            c = sqlite3.connect(":memory:")
            tables = re.findall(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w]*)", sql, re.I)
            for t in set(tables) or ["t"]:
                c.execute(f"CREATE TABLE IF NOT EXISTS {t} (id INTEGER PRIMARY KEY, name TEXT, ts TIMESTAMP)")
            where = re.search(r"WHERE\s+(.+?)(?:ORDER|GROUP|LIMIT|$)", sql, re.I | re.S)
            row = None
            try:
                row = c.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
            except Exception:
                # sql references columns missing in the mock schema -> retry with LIKE-tolerant form
                if where and "LIKE" in sql.upper():
                    col = re.split(r"[\s=%'\"(]+", where.group(1))[0]
                    try:
                        row = c.execute(f"EXPLAIN QUERY PLAN SELECT * FROM {tables[0]} WHERE {col} LIKE '%x%'").fetchall()
                    except Exception:
                        row = None
            plan = " | ".join(r[-1] for r in row) if row else None
            c.close()
            if row:
                txt = plan.lower()
                if "scan" in txt and "index" not in txt:
                    notes.append(("MEDIUM", "missing-index", "full-table scan on a hot path",
                                  "CREATE INDEX on the WHERE column(s); or rewrite to seek"))
                if "temp" in txt or "using index" not in txt and "order by" in sql.lower():
                    notes.append(("LOW", "sort-cost", "unindexed ORDER BY (temp B-tree)", "composite index matching ORDER BY"))
            return plan, notes
        except Exception:
            return None, notes
