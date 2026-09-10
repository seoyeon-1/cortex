"""Phase 16.4 - FormFillerTool: competition intel + user profile -> filled web form / email draft.

Safety stance (non-negotiable):
- default `confirm_before_submit: true` => the tool FILLS but never clicks submit; the
  human reviews the headful browser and presses the button.
- submit automation is additionally gated by config `secretary.form.allow_submit` (false by
  default) - two independent switches must be flipped to ever auto-submit.
- every run writes a mapping report (data/form_filler/last_report.json): which fields were
  filled, with what confidence, and which need manual attention.

Mapping source: LLM structured mapping when reachable; otherwise a deterministic heuristic
(field name/placeholder/label vs profile keys via normalization + fuzzy ratio). Uncertain
fields are NEVER guessed - they land in `manual` for the user.
"""
import difflib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.tools.base import BaseTool, ToolResult

_PROFILE_HINTS = {
    "name": ["name", "이름", "성명", "full name", "성함"],
    "email": ["email", "e-mail", "메일", "이메일", "전자우편"],
    "phone": ["phone", "tel", "연락처", "휴대폰", "전화"],
    "organization": ["organization", "org", "company", "소속", "기관", "회사", "학교"],
    "position": ["position", "직위", "직급", "role", "title"],
    "address": ["address", "주소", "소재지"],
    "bio": ["bio", "intro", "self", "소개", "자기소개", "경력"],
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z가-힣0-9]+", "", (s or "").lower())


class FormFillerTool(BaseTool):
    name = "fill_form"
    description = ("Map a competition JSON (from analyze_competition) + user profile onto a submission "
                   "web form (mode=web_form: fills fields via browser, confidence>=0.8 only, NEVER submits "
                   "unless config allow_submit AND confirm_before_submit=false) or draft the submission "
                   "email (mode=email_draft: returns payload for the email tool). Manual fields reported, not guessed.")
    parameters = {
        "type": "object",
        "properties": {
            "competition_json_path": {"type": "string", "description": "data/competitions/*.json"},
            "user_profile_path": {"type": "string", "description": "default: secretary.user_profile"},
            "submission_url": {"type": "string", "description": "override the JSON's submission_details.url"},
            "mode": {"type": "string", "enum": ["web_form", "email_draft"], "default": "web_form"},
            "confirm_before_submit": {"type": "boolean", "default": True},
            "thought": {"type": "string"},
        },
        "required": ["competition_json_path", "thought"],
    }

    def __init__(self, workspace_root: str, cfg: Optional[Dict] = None, llm_cfg: Optional[Dict] = None):
        self.workspace_root = Path(workspace_root)
        fc = (cfg or {}).get("form", {}) or {}
        self.allow_submit = bool(fc.get("allow_submit", False))
        self.min_confidence = float(fc.get("min_confidence", 0.8))
        self.submit_selector = fc.get("submit_selector", "button[type=submit], input[type=submit]")
        self.report_dir = self.workspace_root / "data" / "form_filler"
        self.profile_path = Path(str((cfg or {}).get("user_profile", "~/.cortex/user_profile.json"))).expanduser()
        self.llm_cfg = llm_cfg or {}
        self._llm = None
        self.governor = None
        self.browser = None
        try:
            from core.tools.secretary.browser import BrowserTool
            self.browser = BrowserTool(workspace_root, cfg)
        except Exception:
            pass

    # ------------------------------------------------------------------ entry

    def execute(self, competition_json_path: str = "", user_profile_path: str = "",
                submission_url: str = "", mode: str = "web_form",
                confirm_before_submit: bool = True, thought: str = "") -> ToolResult:
        comp = self._load_json(competition_json_path)
        if isinstance(comp, ToolResult):
            return comp
        profile = self._load_json(user_profile_path or str(self.profile_path))
        if isinstance(profile, ToolResult):
            return profile
        try:
            if mode == "email_draft":
                return self._email_draft(comp, profile)
            return self._fill_web_form(comp, profile, submission_url, bool(confirm_before_submit))
        except Exception as e:
            return ToolResult(False, error=f"fill_form {mode}: {str(e)[:300]}")

    def _load_json(self, p) -> Any:
        path = Path(str(p)).expanduser()
        if not path.is_absolute():
            path = self.workspace_root / path
        if not path.is_file():
            return ToolResult(False, retryable=False,
                              error=f"fill_form: file not found: {p} (run analyze_competition first for the intel json)")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            return ToolResult(False, retryable=False, error=f"fill_form: bad JSON in {p}: {e}")

    # ---------------------------------------------------------------- web form

    def _fill_web_form(self, comp: Dict, profile: Dict, url_override: str, confirm: bool) -> ToolResult:
        url = url_override or (comp.get("submission_details") or {}).get("url")
        if not url:
            return ToolResult(False, retryable=False,
                              error="no submission url in competition JSON and none given; "
                                    "if submission is email-based use mode=email_draft")
        if self.browser is None:
            return ToolResult(False, error="browser tool unavailable (playwright not installed)")
        nav = self.browser.execute(action="goto", url=url, wait_for="load",
                                   thought=f"open submission form for: {comp.get('title', '')[:60]}")
        if not nav.success:
            return ToolResult(False, error=f"navigate failed: {nav.error}")
        dom_raw = (nav.data or {}).get("dom", "[]")
        try:
            elements = json.loads(dom_raw) if isinstance(dom_raw, str) else dom_raw
        except Exception:
            elements = []
        mappings = self._llm_map(elements, profile, comp) or self._heuristic_map(elements, profile)
        filled, manual = [], []
        for m in mappings:
            conf = float(m.get("confidence", 0) or 0)
            val = m.get("value")
            if conf >= self.min_confidence and val not in (None, "") and m.get("selector"):
                r = self.browser.execute(action="fill", selector=m["selector"], text=str(val),
                                         thought=f"form field {m.get('field','')}")
                (filled if r.success else manual).append({**m, "filled": r.success,
                                                          **({} if r.success else {"reason": r.error[:120]})})
            else:
                manual.append({**m, "reason": m.get("reason", f"confidence {conf:.2f} < {self.min_confidence}")})

        will_submit = (not confirm) and self.allow_submit
        submit_note = "submit NOT clicked - review the headful browser and press submit yourself"
        if will_submit:
            r = self.browser.execute(action="click", selector=self.submit_selector, thought="final submit")
            submit_note = ("AUTO-SUBMITTED" if r.success else f"submit click failed: {r.error[:120]}")
        report = {"url": url, "mode": "web_form", "filled": len(filled), "manual": len(manual),
                  "confirm_before_submit": confirm, "allow_submit_cfg": self.allow_submit,
                  "outcome": submit_note, "mappings": mappings}
        self.report_dir.mkdir(parents=True, exist_ok=True)
        (self.report_dir / "last_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                                           encoding="utf-8")
        summary = (f"form: {len(filled)} field(s) filled, {len(manual)} need manual review. {submit_note}"
                   if not will_submit else f"form submitted. {submit_note}")
        if not confirm and not self.allow_submit:
            summary += " (confirm_before_submit=false ignored: secretary.form.allow_submit=false - SAFETY)"
        return ToolResult(True, data={"filled": filled, "manual": manual,
                                      "report": "data/form_filler/last_report.json"}, summary=summary)

    # --------------------------------------------------------------- mapping

    def _heuristic_map(self, elements: List[Dict], profile: Dict) -> List[Dict]:
        """Deterministic offline mapping: normalized field labels vs profile keys + fuzzy ratio."""
        out = []
        prof_norm = {k: str(v) for k, v in profile.items()
                     if isinstance(v, (str, int, float)) and str(v).strip()}
        for el in elements or []:
            if el.get("tag", "").upper() not in ("INPUT", "TEXTAREA", "SELECT") and "submit" not in str(el.get("type", "")):
                continue
            if el.get("type") in ("submit", "button", "hidden", "checkbox", "radio", "file"):
                continue
            hay = _norm(" ".join(str(el.get(k) or "") for k in ("name", "id", "placeholder", "label", "aria")))
            best, score = None, 0.0
            for key, hints in _PROFILE_HINTS.items():
                if hay and any(_norm(h) in hay for h in hints):
                    best, score = key, 1.0
                    break
            if best is None and hay:
                cands = [(difflib.SequenceMatcher(None, hay, _norm(k)).ratio(), k) for k in prof_norm]
                score, best = max(cands) if cands else (0, None)
            if best and score < self.min_confidence:
                best, score = None, round(score, 2)     # too fuzzy to claim a field identity
            val = prof_norm.get(best or "", None)
            if best and score >= self.min_confidence and val:
                out.append({"selector": el.get("selector"), "field": best, "value": val, "confidence": round(score, 2)})
            else:
                out.append({"selector": el.get("selector"), "field": best, "value": None,
                            "confidence": round(score, 2), "reason": "no confident profile match"})
        return out

    def _llm_map(self, elements: List[Dict], profile: Dict, comp: Dict) -> Optional[List[Dict]]:
        llm = self._get_llm()
        if llm is None:
            return None
        try:
            from core.tools.secretary.competition_intel import extract_json_object
            resp = llm.chat(messages=[{"role": "system",
                                       "content": "Map form fields to profile values. JSON only: "
                                                  '{"mappings":[{"selector":...,"field":...,"value":...,"confidence":0..1}]} '
                                                  "Uncertain -> value null + reason. Never invent identity data."},
                                      {"role": "user", "content": json.dumps(
                                          {"elements": elements[:80], "profile": profile,
                                           "documents": comp.get("required_documents")}, ensure_ascii=False)[:14000]}],
                             tools=None)
            obj = extract_json_object(resp.choices[0].message.content or "") or {}
            maps = obj.get("mappings")
            try:
                g = self.governor
                if g is not None:
                    g.record("form_filler", 3600, 400, llm.model)
            except Exception as ge:
                if "budget" in str(ge).lower():
                    raise
            return maps if isinstance(maps, list) and maps else None
        except Exception:
            return None

    def _get_llm(self):
        if self._llm is None and self.llm_cfg.get("model"):
            try:
                from core.llm.client import LLMClient
                self._llm = LLMClient(self.llm_cfg)
            except Exception:
                return None
        return self._llm

    # ------------------------------------------------------------- email draft

    def _email_draft(self, comp: Dict, profile: Dict) -> ToolResult:
        to = (comp.get("submission_details") or {}).get("email")
        if not to:
            return ToolResult(False, retryable=False,
                              error="competition has no submission email (submission_details.email)")
        docs = comp.get("required_documents") or []
        doc_lines = "\n".join(f"- {d.get('name', d) if isinstance(d, dict) else d}" for d in docs) or "- 참가신청서"
        body = (f"수신: {to}\n\n"
                f"안녕하십니까. {comp.get('host', '주최측')} 담당자님.\n\n"
                f"'{comp.get('title', '')}' 대회에 아래와 같이 참가 신청드립니다.\n\n"
                f"[첨부 서류]\n{doc_lines}\n\n"
                f"신청자: {profile.get('name', '')} ({profile.get('organization', '')} {profile.get('position', '')})\n"
                f"연락처: {profile.get('phone', '')} / 이메일: {profile.get('email', '')}\n\n"
                f"검토 부탁드립니다. 감사합니다.")
        payload = {"to": to,
                   "subject": f"[참가신청] {comp.get('title', '')} - {profile.get('name', '')}",
                   "body": body,
                   "attachments": list((profile.get("attachments") or {}).values()),
                   "deadline": comp.get("deadline"), "send_now": False,
                   "how_to_send": 'email tool: {"action":"send","to":...,"subject":...,"body":...,"attachments":[...]}'
                                  ' (attachments filtered to workspace-visible paths)'}
        self.report_dir.mkdir(parents=True, exist_ok=True)
        (self.report_dir / "last_draft.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                                          encoding="utf-8")
        return ToolResult(True, data=payload,
                          summary=f"email draft ready for {to} (deadline {payload['deadline'] or '?'}); "
                                  f"NOT sent - review then call email.send")
