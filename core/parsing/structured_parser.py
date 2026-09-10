"""Phase 15.2 - Structured output parser.

Native function calling is the primary path. This parser is the SAFETY NET: when a
(model sometimes replies in plain text) the model returns text instead of tool_calls,
we recover a protocol-conformant reply ("Thought: ... Plan: ... ToolCalls: [...JSON...]")
into real tool calls instead of burning an iteration on the generic "You MUST use tools"
nudge. If the reply is not protocol-conformant we return a precise retry prompt telling
the model exactly which section is broken.

Also renders the format error hint for the force-action retry message.
"""
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_THOUGHT_RE = re.compile(r"^Thought:\s*(.+?)(?=^\s*Plan:|^\s*ToolCalls:|\Z)", re.DOTALL | re.MULTILINE | re.IGNORECASE)
_PLAN_RE = re.compile(r"^Plan:\s*(.+?)(?=^\s*ToolCalls:|\Z)", re.DOTALL | re.MULTILINE | re.IGNORECASE)
_TOOLS_RE = re.compile(r"^\s*ToolCalls:\s*(.+?)\s*(?:\n\s*(?:Thought|Plan):|\Z)", re.DOTALL | re.MULTILINE | re.IGNORECASE)


@dataclass
class StructuredResponse:
    thought: str = ""
    plan: List[str] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)   # format violations (fixable by retry)
    raw: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.tool_calls)


class StructuredOutputParser:
    """Parse/reject the CORTEX REASONING PROTOCOL reply format (prompts/react_structured.md)."""

    def parse(self, text: str) -> StructuredResponse:
        text = text or ""
        resp = StructuredResponse(raw=text)
        if not text.strip():
            resp.errors.append("empty response")
            return resp

        t = _THOUGHT_RE.search(text)
        p = _PLAN_RE.search(text)
        c = _TOOLS_RE.search(text)

        if not t:
            resp.errors.append("missing 'Thought:' section")
        else:
            resp.thought = t.group(1).strip()
        if not p:
            resp.errors.append("missing 'Plan:' section")
        else:
            resp.plan = [re.sub(r"^\s*\d+[.)]?\s+", "", ln).strip() for ln in p.group(1).strip().splitlines() if ln.strip()]
        if not c:
            resp.errors.append("missing 'ToolCalls:' section")
        else:
            calls, err = self._extract_json_array(c.group(1))
            if err:
                resp.errors.append(f"ToolCalls JSON invalid: {err}")
            else:
                resp.tool_calls = self._validate_calls(calls, resp.errors)
        # protocol rule 2: plan must match tool count (loose check -> nudge, not hard fail)
        if resp.plan and resp.tool_calls and len(resp.plan) < len(resp.tool_calls):
            resp.errors.append("Plan shorter than ToolCalls - every call needs a plan step")
        return resp

    # -------------------------------------------------------------- internals

    @staticmethod
    def _extract_json_array(blob: str):
        """Find the JSON array: try fenced block, then balanced/repair parse."""
        m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", blob, re.DOTALL)
        candidates = []
        if m:
            candidates.append(m.group(1))
        start = blob.find("[")
        if start != -1:
            candidates.append(blob[start:])
        for cand in candidates:
            for attempt in (cand, cand + "]"):        # tolerate a dropped closing bracket
                try:
                    return json.loads(attempt), None
                except json.JSONDecodeError:
                    continue
            # last resort: salvage individual {...} objects
            objs = []
            for om in re.finditer(r"\{(?:[^{}]|\{[^{}]*\})*\}", cand):
                try:
                    objs.append(json.loads(om.group(0)))
                except Exception:
                    pass
            if objs:
                return objs, None
            return None, "no parseable JSON array"
        return None, "no '[' found"

    @staticmethod
    def _validate_calls(calls, errors: List[str]) -> List[Dict[str, Any]]:
        out = []
        for i, tc in enumerate(calls if isinstance(calls, list) else [calls]):
            if not isinstance(tc, dict) or not tc.get("name"):
                errors.append(f"tool_call[{i}] must be {{\"name\":..., \"arguments\":{{...}}}}")
                continue
            args = tc.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    errors.append(f"tool_call[{i}].arguments is not valid JSON")
                    continue
            if not isinstance(args, dict):
                errors.append(f"tool_call[{i}].arguments must be an object")
                continue
            out.append({"name": str(tc["name"]), "arguments": args})
        return out

    def retry_prompt(self, reason: str) -> str:
        return ("# FORMAT ERROR: " + reason + "\n"
                "Your reply MUST follow the CORTEX REASONING PROTOCOL exactly:\n"
                "Thought: <root-cause reasoning citing tests/line numbers>\n"
                "Plan: <numbered steps for THIS turn>\n"
                "ToolCalls: <valid JSON array>\n"
                'Example:\nThought: test_divide_by_zero fails; divide() raises ZeroDivisionError.\n'
                'Plan: ["1. edit_file(calculator.py) to guard b==0"]\n'
                'ToolCalls: [{"name": "edit_file", "arguments": {"path": "calculator.py", '
                '"unified_diff": "--- a/calculator.py\\n+++ b/calculator.py\\n@@ -1,2 +1,3 @@\\n '
                'def divide(a, b):\\n+    if b == 0: return None\\n     return a / b\\n", '
                '"thought": "add zero guard"}}}]')

    def build_native_calls(self, resp: StructuredResponse) -> List[Any]:
        """Wrap recovered calls so the orchestrator can execute them like real tool_calls."""
        import types
        out = []
        for i, tc in enumerate(resp.tool_calls):
            fn = types.SimpleNamespace(name=tc["name"], arguments=json.dumps(tc["arguments"], ensure_ascii=False))
            out.append(types.SimpleNamespace(id=f"structured-{i}", type="function", function=fn))
        return out
