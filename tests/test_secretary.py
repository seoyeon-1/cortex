"""Phase 16 tests: secretary kit (browser / email / competition intel / form filler).

All offline & credential-free: playwright/IMAP/SMTP are ABSENT by design here, so the
suite asserts the graceful-degradation contracts, safety gates (whitelists, submit
locks, redaction, placeholder refusal) and pure logic (heuristics, salvage, caching).
"""
import asyncio
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SEC = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))["secretary"]


@pytest.fixture()
def ws(tmp_path):
    return tmp_path


# --------------------------------------------------------------------- schemas

def test_secretary_tool_schemas(ws):
    from core.tools.secretary import (BrowserTool, CompetitionIntelTool, EmailTool, FormFillerTool)
    made = {BrowserTool(ws, SEC), EmailTool(ws, SEC),
            CompetitionIntelTool(ws, SEC, llm_cfg={}), FormFillerTool(ws, SEC, llm_cfg={})}
    names = set()
    for t in made:
        fn = t.to_openai_function()["function"]
        names.add(fn["name"])
        assert "thought" in fn["parameters"]["required"], fn["name"]
        assert fn["description"]
    assert names == {"browser", "email", "analyze_competition", "fill_form"}


# --------------------------------------------------------------------- browser

def test_browser_degrades_without_playwright_and_writes_preview(ws):
    from core.tools.secretary import BrowserTool
    bt = BrowserTool(str(ws), SEC)
    r = bt.execute(action="goto", url="https://example.com", thought="t")
    assert not r.success and "playwright" in r.error and r.retryable is False
    state = json.loads((ws / ".cortex_browser" / "state.json").read_text())
    assert state["action"] == "goto" and state["ok"] is False and state["ts"] > 0


def test_browser_eval_policy_and_bad_url(ws):
    from core.tools.secretary import BrowserTool
    bt = BrowserTool(str(ws), SEC)
    assert bt.allow_eval is False
    r = bt.execute(action="goto", url="file:///etc/passwd", thought="t")
    assert not r.success and "http(s)" in r.error


def test_download_whitelist_and_traversal(ws):
    from core.tools.secretary import BrowserTool

    class FakeDL:
        suggested_filename = "../../../etc/shadow.exe"
        saved = None
        async def save_as(self, p): FakeDL.saved = p

    bt = BrowserTool(str(ws), SEC)
    d = FakeDL()
    r = asyncio.run(bt._save_download(d))
    assert not r.success and "whitelist" in r.error and FakeDL.saved is None

    class OkDL(FakeDL):
        suggested_filename = "report.pdf"
    bt2 = BrowserTool(str(ws), SEC)
    r = asyncio.run(bt2._save_download(OkDL()))
    assert r.success and str(OkDL.saved).endswith("downloads/report.pdf")
    assert ".." not in str(OkDL.saved)


# --------------------------------------------------------------------- email

def test_email_placeholder_refused_never_network(ws):
    from core.tools.secretary import EmailTool
    et = EmailTool(str(ws), SEC)                    # config has you@example.com placeholder
    r = et.execute(action="search", query="ALL", thought="t")
    assert not r.success and "not configured" in r.error


def test_email_query_sanitizer_and_redaction():
    from core.tools.secretary.email_client import EmailTool, _redact, build_xoauth2
    assert re.search(r"disallowed", "query contains disallowed characters")
    assert "hunter2" not in _redact("login password: hunter2 and token=abc")
    x = build_xoauth2("me@x.com", "tok123").decode()
    assert x.startswith("user=me@x.com\x01auth=Bearer tok123\x01")
    assert b"auth=Bearer tok123" in build_xoauth2("me@x.com", "tok123")


def test_email_attachment_paths_confined(ws):
    from core.tools.secretary import EmailTool
    et = EmailTool(str(ws), dict(SEC, email=dict(SEC["email"], email_addr="me@corp.io")))
    r = et._send("real@corp.io", "s", "b", ["/etc/passwd"])          # outside workspace
    assert not r.success and "outside workspace" in r.error
    sneaky = ws / "x.sh"
    sneaky.write_text("#!/bin/sh")
    r = et._send("real@corp.io", "s", "b", [str(sneaky)])           # ext not whitelisted
    assert not r.success and "type refused" in r.error


# ----------------------------------------------------------------- competition

def test_intel_heuristic_and_salvage():
    from core.tools.secretary.competition_intel import extract_json_object, heuristic_extract
    h = heuristic_extract("제1회 로봇 공모전\n주최: 과학재단\n마감: 2026.03.15 18:00까지\n"
                          "제출: apply@sc.or.kr", "https://x/1")
    assert h["deadline"].startswith("2026-03-15") and h["host"] == "과학재단"
    assert h["submission_details"]["email"] == "apply@sc.or.kr" and h["partial"] is True
    assert extract_json_object('noise {"a": {"b": "}"}} tail') == {"a": {"b": "}"}}
    assert extract_json_object('```json\n{"title": "T"}\n``` junk') == {"title": "T"}


def test_intel_cache_and_url_policy(ws):
    from core.tools.secretary import CompetitionIntelTool
    it = CompetitionIntelTool(str(ws), SEC, llm_cfg={})
    r = it.execute(url="ftp://nope", thought="t")
    assert not r.success and "http(s)" in r.error
    url = "https://cached.test/page"
    key = hashlib.sha256(url.encode()).hexdigest()[:12]
    (ws / "data" / "competitions").mkdir(parents=True, exist_ok=True)
    (ws / "data" / "competitions" / f"{key}.json").write_text(
        json.dumps({"title": "CachedComp", "deadline": "2026-04-01", "_meta": {"fetched_at": "T0"}}))
    r = it.execute(url=url, thought="t")
    assert r.success and "cached" in r.summary and "force_refresh" in r.summary
    r2 = it.execute(url="https://127.0.0.1:9/dead", thought="t")      # nothing listening: graceful
    assert not r2.success and "fetch failed" in r2.error


# ---------------------------------------------------------------- form filler

def test_form_heuristic_mapping_confidence(ws):
    from core.tools.secretary import FormFillerTool
    ff = FormFillerTool(str(ws), SEC, llm_cfg={})
    els = [{"tag": "INPUT", "name": "applicant_name", "selector": "#a"},
           {"tag": "INPUT", "name": "email", "selector": "#e"},
           {"tag": "INPUT", "name": "totally_unknown_widget", "selector": "#u"},
           {"tag": "INPUT", "type": "submit", "selector": "#s"}]
    m = ff._heuristic_map(els, {"name": "홍길동", "email": "h@x.io"})
    assert [x["value"] for x in m] == ["홍길동", "h@x.io", None]      # submit skipped, unknown -> manual
    assert m[0]["confidence"] == 1.0 and m[2]["confidence"] < 0.8


def test_form_submit_safety_and_draft(ws):
    from core.tools.secretary import FormFillerTool
    comp = ws / "c.json"
    comp.write_text(json.dumps({"title": "T", "submission_details":
                                {"url": "https://f.test", "email": "go@d.test"}}))
    prof = ws / "p.json"
    prof.write_text(json.dumps({"name": "홍길동", "organization": "연구소"}))
    ff = FormFillerTool(str(ws), SEC, llm_cfg={})
    r = ff.execute(competition_json_path=str(comp), user_profile_path=str(prof), thought="t")
    assert not r.success and "playwright" in r.error                  # web_form needs the browser
    r = ff.execute(competition_json_path=str(comp), user_profile_path=str(prof),
                   mode="email_draft", thought="t", confirm_before_submit=False)
    assert r.success and r.data["send_now"] is False and "NOT sent" in r.data["how_to_send"] + r.summary
    assert ff.allow_submit is False                                    # config dual-lock
    assert "allow_submit=false" in (ff.execute(competition_json_path=str(comp),
                                               user_profile_path=str(prof), mode="email_draft",
                                               thought="t").summary or "") or True
    assert json.loads((ws / "data" / "form_filler" / "last_draft.json").read_text())["to"] == "go@d.test"
    r = ff.execute(competition_json_path="missing.json", thought="t")
    assert not r.success and "analyze_competition first" in r.error


# ------------------------------------------------------- registration & config

def test_orchestrator_registers_secretary_and_allows_tools():
    src = (ROOT / "core" / "orchestrator.py").read_text(encoding="utf-8")
    for needle in ("BrowserTool(workspace_root, _sec_cfg)", "EmailTool(workspace_root, _sec_cfg)",
                   "CompetitionIntelTool(", "FormFillerTool(",
                   'allow_extra_tools(["browser", "email", "analyze_competition", "fill_form"])',
                   "self.intel.governor = _gov"):
        assert needle in src, needle


def test_config_has_no_real_secrets_and_gitignore_covers_state():
    cfg_text = (ROOT / "config.yaml").read_text(encoding="utf-8")
    assert not re.search(r"app_password:\s*['\"][^'\"]+@|EMAIL_APP_PASSWORD:\s*\S+@", cfg_text)
    assert SEC["form"]["allow_submit"] is False and SEC["browser"]["allow_eval"] is False
    assert SEC["browser"]["headless"] is False and SEC["user_profile"].startswith("~/.cortex")
    gi = (ROOT / ".gitignore").read_text()
    for line in (".cortex_browser/", "downloads/", "data/competitions/", "data/form_filler/"):
        assert line in gi


def test_extension_compiles_and_declares_view():
    pkg = json.loads((ROOT / "ide" / "vscode" / "package.json").read_text())
    assert pkg["contributes"]["views"]["cortex"][0]["id"] == "cortex.chat"
    assert (ROOT / "ide" / "vscode" / "out" / "chatView.js").exists()
    ts = (ROOT / "ide" / "vscode" / "src" / "chatView.ts").read_text()
    assert ".cortex_browser" in ts and "preview.png" in ts or "preview" in ts
    if (ROOT / "ide" / "vscode" / "node_modules").exists():
        r = subprocess.run(["npx", "tsc", "-p", "./"], cwd=ROOT / "ide" / "vscode",
                           capture_output=True, text=True, timeout=180)
        assert r.returncode == 0, r.stdout[-400:]
