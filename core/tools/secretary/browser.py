"""Phase 16.1 - BrowserTool: Playwright persistent-context automation for the secretary.

Design notes vs. a naive wrapper:
- Persistent profile (user_data_dir) keeps logins/cookies/2FA across runs. Default headless;
  set headless:false for interactive tasks (captcha, final submission eyeball).
- `launch_persistent_context` returns a BrowserContext (not a Browser) - held as one object.
- Playwright import is LAZY and every failure is a clean ToolResult, never an exception:
  the agent loop keeps working on boxes without the browser installed.
- After EVERY action (success or failure) the tool writes
      <workspace>/.cortex_browser/preview.png   and   .cortex_browser/state.json
  so the VS Code ChatViewProvider can live-poll the screenshot without any IPC.
- Downloads land in <workspace>/downloads with sanitized filenames + extension whitelist.
- Optional politeness delay (rate_limit_sec) on navigation, per anti-BAN guidance.
"""
import asyncio
import json
import random
import time
from pathlib import Path
from typing import Any, Dict, Optional

from core.tools.base import BaseTool, ToolResult

ALLOWED_DOWNLOAD_EXT = {"pdf", "zip", "png", "jpg", "jpeg", "gif", "hwp", "doc", "docx",
                        "xls", "xlsx", "pptx", "csv", "txt", "json", "xml", "gz", "tar"}
_DOM_SNAPSHOT_JS = """() => {
  const els = document.querySelectorAll('a,button,input,select,textarea,[role="button"],[onclick],[type="submit"]');
  return Array.from(els).slice(0, 160).map((el, i) => ({
    i, tag: el.tagName, id: el.id || undefined, name: el.name || undefined, type: el.type || undefined,
    placeholder: el.placeholder || undefined, aria: el.getAttribute('aria-label') || undefined,
    label: (el.labels && el.labels[0]) ? el.labels[0].innerText.slice(0, 60) : undefined,
    text: (el.innerText || '').slice(0, 100) || undefined, href: el.href || undefined,
    selector: el.id ? '#' + el.id : (el.name ? '[name="' + el.name + '"]' : (el.tagName + ':nth-of-type(' + (i + 1) + ')'))
  }));
}"""


class BrowserTool(BaseTool):
    name = "browser"
    description = ("Stateful web browser (Playwright persistent context - logins/cookies survive). "
                   "Actions: goto|click|fill|scroll|press|screenshot|get_dom|download|wait|eval|close. "
                   "Every action refreshes .cortex_browser/preview.png for the IDE sidebar. "
                   "Never auto-submits forms; interactive steps (captcha/2FA) run in headless:false mode.")
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["goto", "click", "fill", "scroll", "press",
                                                   "screenshot", "get_dom", "download", "wait",
                                                   "eval", "close"]},
            "url": {"type": "string", "description": "for goto"},
            "selector": {"type": "string", "description": "CSS selector for click/fill/download-trigger"},
            "text": {"type": "string", "description": "value for fill / key for press"},
            "delta": {"type": "integer", "description": "scroll pixels (default 500)"},
            "ms": {"type": "integer", "description": "wait milliseconds (default 1000)"},
            "script": {"type": "string", "description": "JS for eval (requires allow_eval)"},
            "wait_for": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle"]},
            "thought": {"type": "string", "description": "why this action"},
        },
        "required": ["action", "thought"],
    }

    # session state is process-global so it survives agent-loop re-instantiation
    _ctx = None
    _pw = None
    _page = None

    def __init__(self, workspace_root: str, cfg: Optional[Dict] = None):
        c = (cfg or {}).get("browser", {}) or {}
        self.workspace_root = Path(workspace_root)
        self.preview_dir = self.workspace_root / ".cortex_browser"
        self.downloads_dir = self.workspace_root / "downloads"
        self.user_data_dir = Path(str(c.get("user_data_dir", "~/.cortex_browser_profile"))).expanduser()
        self.headless = bool(c.get("headless", True))
        self.locale = c.get("locale", "ko-KR")
        self.timezone = c.get("timezone", "Asia/Seoul")
        self.nav_timeout = int(c.get("timeout_sec", 60))
        self.rate_limit_sec = float(c.get("rate_limit_sec", 1.5))
        self.allow_eval = bool(c.get("allow_eval", False))
        self.send_b64 = bool(c.get("send_b64", False))
        self.viewport = {"width": int(c.get("viewport_width", 1280)),
                         "height": int(c.get("viewport_height", 800))}
        self.profile_path = Path(str((cfg or {}).get("user_profile",
                                    "~/.cortex/user_profile.json"))).expanduser()

    # ----------------------------------------------------------------- entry

    def execute(self, action: str = "", thought: str = "", **kwargs) -> ToolResult:
        if not action:
            return ToolResult(False, error="browser: 'action' required",
                              summary="actions: goto|click|fill|scroll|press|screenshot|get_dom|download|wait|eval|close")
        if action == "goto" and not str(kwargs.get("url", "")).startswith(("http://", "https://")):
            return ToolResult(False, retryable=False,
                              error=f"browser goto: only http(s) urls allowed, got {kwargs.get('url')!r}")
        try:
            return self._run(action, kwargs)
        except Exception as e:
            # honest failure + preview state so the IDE shows what went wrong
            self._write_preview({"url": getattr(self._page, "url", ""), "action": action,
                                 "ok": False, "error": str(e)[:400]})
            return ToolResult(False, error=f"browser {action}: {type(e).__name__}: {str(e)[:400]}",
                              retryable=False)

    def _run(self, action: str, kw: Dict[str, Any]) -> ToolResult:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        coro = self._async_run(action, kw)
        if loop and loop.is_running():                      # embedded in a live event loop
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(1) as ex:
                return ex.submit(lambda: asyncio.run(coro)).result()
        return asyncio.run(coro)

    # ----------------------------------------------------------------- async

    async def _ensure(self):
        if BrowserTool._page is not None and not BrowserTool._page.is_closed():
            return BrowserTool._page
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError("playwright not installed - run: pip install playwright && playwright install chromium")
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        BrowserTool._pw = await async_playwright().start()
        BrowserTool._ctx = await BrowserTool._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.user_data_dir), headless=self.headless, viewport=self.viewport,
            locale=self.locale, timezone_id=self.timezone,
            args=["--disable-blink-features=AutomationControlled"])
        BrowserTool._ctx.set_default_timeout(self.nav_timeout * 1000)
        BrowserTool._page = (BrowserTool._ctx.pages or [None])[0] or await BrowserTool._ctx.new_page()
        return BrowserTool._page

    async def _async_run(self, action: str, kw: Dict[str, Any]) -> ToolResult:
        if action == "close":
            return await self._close()
        page = await self._ensure()
        if action == "goto":
            url = kw.get("url", "")
            if not url.startswith(("http://", "https://")):
                return ToolResult(False, error=f"browser goto: only http(s) urls allowed, got {url!r}",
                                  retryable=False)
            if self.rate_limit_sec > 0:
                await asyncio.sleep(random.uniform(0.5, self.rate_limit_sec))   # anti-BAN politeness
            await page.goto(url, wait_until=kw.get("wait_for", "domcontentloaded"),
                             timeout=self.nav_timeout * 1000)
        elif action == "click":
            await page.click(kw["selector"], timeout=15000)
        elif action == "fill":
            await page.fill(kw["selector"], str(kw.get("text", "")), timeout=10000)
        elif action == "press":
            key = kw.get("text") or kw.get("key") or "Enter"
            if kw.get("selector"):
                await page.press(kw["selector"], key)
            else:
                await page.keyboard.press(key)
        elif action == "scroll":
            await page.evaluate(f"window.scrollBy(0, {int(kw.get('delta', 500))})")
        elif action == "wait":
            await page.wait_for_timeout(int(kw.get("ms", 1000)))
        elif action == "eval":
            if not self.allow_eval:
                return ToolResult(False, error="browser eval disabled by policy "
                                               "(secretary.browser.allow_eval=false)", retryable=False)
            out = await page.evaluate(kw.get("script", "1+1"))
            return ToolResult(True, data=out, summary="eval ok")
        elif action == "download":
            return await self._download(page, kw)

        state = await self._capture_state(page, action)
        data = {"url": state["url"], "title": state["title"], "dom": state["dom_snapshot"][:12000]}
        if self.send_b64:
            data["screenshot_b64"] = state["screenshot_b64"]
        return ToolResult(True, data=data,
                          summary=f"browser {action} ok: {state['url'][:120]} | {state['title'][:60]}")

    async def _download(self, page, kw) -> ToolResult:
        async with page.expect_download(timeout=30000) as di:
            await page.click(kw["selector"])
        d = await di.value
        return await self._save_download(d)

    async def _save_download(self, download) -> ToolResult:
        safe = Path(str(download.suggested_filename)).name            # strip any path components
        ext = safe.rsplit(".", 1)[-1].lower() if "." in safe else ""
        if ext not in ALLOWED_DOWNLOAD_EXT:
            return ToolResult(False, error=f"download blocked: '.{ext}' not in whitelist",
                              retryable=False)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        dest = self.downloads_dir / safe
        await download.save_as(str(dest))
        return ToolResult(True, data=str(dest), summary=f"downloaded -> downloads/{safe}")

    async def _capture_state(self, page, action: str) -> Dict[str, Any]:
        png = b""
        dom = "[]"
        try:
            png = await page.screenshot(full_page=False, type="png")
        except Exception:
            pass
        try:
            dom = json.dumps(await page.evaluate(_DOM_SNAPSHOT_JS), ensure_ascii=False)
        except Exception:
            pass
        state = {"url": page.url, "title": await page.title() or "", "action": action, "ok": True,
                 "dom_snapshot": dom, "screenshot_b64": ""}
        import base64
        state["screenshot_b64"] = base64.b64encode(png).decode() if png else ""
        self._write_preview(state, png)
        return state

    async def _close(self) -> ToolResult:
        try:
            if BrowserTool._ctx:
                await BrowserTool._ctx.close()
            if BrowserTool._pw:
                await BrowserTool._pw.stop()
        finally:
            BrowserTool._ctx = BrowserTool._page = BrowserTool._pw = None
        self._write_preview({"url": "", "title": "", "action": "close", "ok": True})
        return ToolResult(True, summary="browser session closed (profile kept for next run)")

    # ------------------------------------------------------------- preview IO

    def _write_preview(self, state: Dict[str, Any], png: bytes = b""):
        """Atomic-ish update for the IDE webview poller; never raises."""
        try:
            self.preview_dir.mkdir(parents=True, exist_ok=True)
            if png:
                (self.preview_dir / "preview.png").write_bytes(png)
            meta = {k: v for k, v in state.items() if k in ("url", "title", "action", "ok", "error")}
            meta["ts"] = time.time()
            meta["preview"] = "preview.png" if png else None
            (self.preview_dir / "state.json").write_text(json.dumps(meta, ensure_ascii=False),
                                                          encoding="utf-8")
        except Exception:
            pass
