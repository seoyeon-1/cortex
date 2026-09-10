"""Phase 16.3 - CompetitionIntelTool: announcement URL -> structured JSON.

Pipeline: render (BrowserTool if available, stdlib http fallback otherwise) -> readable
text -> LLM structured extraction (forced JSON) -> salvage parse -> cached artifact under
workspace data/competitions/ for the FormFiller. When no LLM is reachable the tool still
returns a HEURISTIC result (Korean/ISO deadline patterns, submission email, org) - an
advisory partial, clearly flagged, never a fabricated "full" extraction.
"""
import hashlib
import json
import re
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.tools.base import BaseTool, ToolResult

_SCHEMA_KEYS = ["title", "host", "summary", "deadline", "eligibility", "required_documents",
                "submission_method", "submission_details", "evaluation_criteria", "prizes",
                "contact", "schedule", "notes"]


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg"}

    def __init__(self):
        super().__init__()
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Salvage the first balanced {...} JSON object (fence-tolerant)."""
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates: List[str] = []
    if m:
        candidates.append(m.group(1))
    if "{" in text:
        candidates.append(text[text.find("{"):])
    for blob in candidates:
        try:
            v = json.loads(blob)
            if isinstance(v, dict):
                return v
        except Exception:
            pass
    # manual brace balance (strings-aware)
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, min(len(text), start + 60000)):
            c = text[i]
            if in_str:
                esc = (c == "\\" and not esc) if c != '"' else (False if not esc else esc)
                if c == '"' and not esc:
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(text[start:i + 1])
                        if isinstance(v, dict):
                            return v
                    except Exception:
                        break
        start = text.find("{", start + 1)
    return None


def heuristic_extract(page_text: str, url: str) -> Dict[str, Any]:
    """No-LLM fallback: deadlines/email/org via patterns. Partial and honest."""
    t = page_text or ""
    out: Dict[str, Any] = {"title": "", "deadline": None, "submission_details": {}, "notes": "",
                           "required_documents": [], "eligibility": [], "source_url": url}
    first_lines = [ln for ln in t.splitlines() if len(ln) > 3][:3]
    out["title"] = first_lines[0][:120] if first_lines else url
    m = (re.search(r"(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일[^(]{0,40}?(\d{1,2})?\s*시?", t)
         or re.search(r"(20\d{2})[-.](\d{1,2})[-.](\d{1,2})\D{0,30}?(?:18|23|24)?[:.]?\d{0,2}\s*(?:까지|마감|D-)?", t))
    if m:
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4)) if (m.lastindex or 0) >= 4 and m.group(4) else None
        out["deadline"] = f"{y}-{mo:02d}-{d:02d}" + (f" {hh:02d}:00" if hh else "")
    em = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", t)
    if em:
        out["submission_details"]["email"] = em.group(0)
    km = re.search(r"(주최|주관)\s*[:\|]?\s*([^\n,]{2,40})", t)
    if km:
        out["host"] = km.group(2).strip()
    if re.search(r"(제출|서류|신청서|계획서)", t):
        out["notes"] = "submit-like keywords present - run full LLM extraction for document list"
    out["partial"] = True
    return out


class CompetitionIntelTool(BaseTool):
    name = "analyze_competition"
    description = ("Fetch a competition/announcement URL and extract structured intel: title, host, "
                   "deadline, eligibility, required_documents, submission_method/details, criteria, "
                   "prizes, contact, schedule. Renders via the browser session when available (JS pages), "
                   "HTTP fallback otherwise, LLM forced-JSON output with heuristic fallback. "
                   "Result cached under data/competitions/ for fill_form.")
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "announcement page URL (http/https)"},
            "force_refresh": {"type": "boolean", "default": False},
            "thought": {"type": "string"},
        },
        "required": ["url", "thought"],
    }

    def __init__(self, workspace_root: str, cfg: Optional[Dict] = None,
                 llm_cfg: Optional[Dict] = None, cortex_root: str = ""):
        self.workspace_root = Path(workspace_root)
        self.cfg = (cfg or {}).get("intel", {}) or {}
        self.llm_cfg = llm_cfg or {}
        self._llm = None                      # lazy: construction must never fail the loop
        self.governor = None                  # injected by orchestrator (cost accounting)
        self.cache_dir = self.workspace_root / "data" / "competitions"
        self.browser = None
        try:
            from core.tools.secretary.browser import BrowserTool
            self.browser = BrowserTool(workspace_root, cfg)
        except Exception:
            pass

    # ------------------------------------------------------------------ entry

    def execute(self, url: str = "", force_refresh: bool = False, thought: str = "") -> ToolResult:
        if not url.startswith(("http://", "https://")):
            return ToolResult(False, error=f"analyze_competition: http(s) URL required, got {url!r}",
                              retryable=False)
        key = hashlib.sha256(url.encode()).hexdigest()[:12]
        cached = self.cache_dir / f"{key}.json"
        if cached.exists() and not force_refresh:
            try:
                obj = json.loads(cached.read_text(encoding="utf-8"))
                return ToolResult(True, data=obj,
                                  summary=f"cached intel for {obj.get('title', '')[:60]} "
                                          f"(fetched {obj.get('_meta', {}).get('fetched_at', '?')}; "
                                          f"force_refresh=true to re-analyze)")
            except Exception:
                pass

        page_text, mode, err = self._fetch_text(url)
        if page_text is None:
            return ToolResult(False, error=f"analyze_competition fetch failed ({err})", retryable=False)

        parsed = self._llm_extract(page_text, url)
        if parsed is None:
            parsed = heuristic_extract(page_text, url)
            parsed["notes"] = (parsed.get("notes", "") + f" | mode={mode}; LLM unavailable - "
                               f"heuristic partial result, not a full extraction").strip(" |")
        parsed.setdefault("source_url", url)
        parsed["_meta"] = {"fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "mode": mode,
                           "chars": len(page_text), "url": url}
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
        dl = parsed.get("deadline") or "?"
        return ToolResult(True, data=parsed,
                          summary=f"competition intel [{mode}] '{str(parsed.get('title'))[:60]}' "
                                  f"deadline={dl} -> {cached.relative_to(self.workspace_root)}")

    # ------------------------------------------------------------------- fetch

    def _fetch_text(self, url: str):
        """-> (text, mode, err). Browser first (JS pages), urllib fallback."""
        if self.browser is not None:
            try:
                r = self.browser.execute(action="goto", url=url, wait_for="load",
                                         thought="competition intel render")
                if r.success:
                    dom = (r.data or {}).get("dom", "[]")
                    text = dom
                    for _ in range(int(self.cfg.get("scroll_passes", 3))):
                        self.browser.execute(action="scroll", delta=1400, thought="lazy-load")
                        self.browser.execute(action="wait", ms=int(self.cfg.get("scroll_wait_ms", 400)))
                    r2 = self.browser.execute(action="eval", script="document.body.innerText",
                                              thought="readable text")
                    if r2.success and isinstance(r2.data, str) and len(r2.data) > len(text):
                        text = r2.data
                    if not r2.success:
                        text = re.sub(r"\s+", " ", dom)[:40000]     # element summary as text
                    return text[:60000], "browser", None
            except Exception:
                pass
        try:                                                                  # stdlib fallback
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (cortex-secretary)"})
            with urllib.request.urlopen(req, timeout=int(self.cfg.get("fetch_timeout_sec", 20))) as r:
                raw = r.read(4_000_000).decode(r.headers.get_content_charset() or "utf-8", "replace")
            p = _TextExtractor()
            p.feed(raw)
            return " \n".join(p.parts)[:60000], "http", None
        except Exception as e:
            return None, "http", str(e)[:200]

    # ------------------------------------------------------------------- llm

    def _get_llm(self):
        if self._llm is None and self.llm_cfg.get("model"):
            from core.llm.client import LLMClient
            self._llm = LLMClient(self.llm_cfg)
        return self._llm

    def _llm_extract(self, page_text: str, url: str) -> Optional[Dict[str, Any]]:
        llm = None
        try:
            llm = self._get_llm()
            if llm is None:
                return None
            keys = ", ".join(f'"{k}": ...' for k in _SCHEMA_KEYS)
            resp = llm.chat(messages=[{"role": "system",
                                       "content": "You extract competition/announcement metadata. "
                                                  "Reply with ONLY one JSON object, no prose, no markdown fences."},
                                      {"role": "user",
                                       "content": f"URL: {url}\nKeys: {keys}\n"
                                                  "deadline as 'YYYY-MM-DD HH:MM' (KST) or null. Unknown -> null/[] . "
                                                  f"PAGE TEXT:\n{page_text[:15000]}"}],
                             tools=None)
            try:                                                              # feed usage to the loop governor
                g = self.governor
                if g is not None:
                    u = getattr(getattr(resp, "usage", None), "prompt_tokens", 0) or len(page_text) // 4
                    c = getattr(getattr(resp, "usage", None), "completion_tokens", 0) or 200
                    g.record("competition_intel", u, c, llm.model)
            except Exception as ge:
                if "budget" in str(ge).lower() or "governor" in str(ge).lower():
                    raise
            out = resp.choices[0].message.content or ""
            obj = extract_json_object(out)
            if obj and any(obj.get(k) for k in _SCHEMA_KEYS):
                return obj
        except Exception:
            return None
        return None
