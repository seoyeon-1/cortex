"""Phase 16.2 - EmailTool: IMAP/SMTP with OAuth2 (XOAUTH2) first, app-password as fallback.

Credential model (never stored in git; config keeps placeholders only):
  priority 1: env var named by `access_token_env` (a fresh OAuth2 access token)
  priority 2: token file `token_file` JSON {refresh_token, client_id, client_secret,
            token_uri, expires_at,...} - auto-refreshed over stdlib urllib when expired
  priority 3: app password from env `EMAIL_APP_PASSWORD` (or config app_password placeholder)

Safety: XOAUTH2 strings are built in-memory and never echoed; errors are redacted for
password/token-shaped substrings; attachment downloads reuse the browser whitelist rules;
send() only attaches files that exist under the workspace/downloads or data/ trees.
"""
import base64
import json
import os
import re
import time
import urllib.parse
import urllib.request
from email.header import decode_header, make_header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Dict, List, Optional

from core.tools.base import BaseTool, ToolResult

PROVIDERS = {
    "gmail":   {"imap": ("imap.gmail.com", 993), "smtp": ("smtp.gmail.com", 465),
                "token_uri": "https://oauth2.googleapis.com/token"},
    "outlook": {"imap": ("outlook.office365.com", 993), "smtp": ("smtp.office365.com", 587),
                "token_uri": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
                "scope": "https://outlook.office365.com/IMAP.AccessAsUser.All"},
}
ATTACH_EXT_WHITELIST = {"pdf", "zip", "png", "jpg", "jpeg", "hwp", "doc", "docx",
                        "xls", "xlsx", "pptx", "csv", "txt"}
_SECRET_RE = re.compile(r"(password|passwd|secret|token|bearer)\S*\s*[:=]\s*\S+", re.IGNORECASE)


def _redact(s: str) -> str:
    return _SECRET_RE.sub(lambda m: m.group(0).split("=")[0].split(":")[0].strip() + ": [REDACTED]", str(s))


def build_xoauth2(user: str, access_token: str) -> bytes:
    """SASL XOAUTH2 initial response (RFC 7628)."""
    return f"user={user}\x01auth=Bearer {access_token}\x01\x01".encode()


class EmailTool(BaseTool):
    name = "email"
    description = ("Secretary mailbox: search|read|send|download_attachments over IMAP/SMTP. "
                   "OAuth2 XOAUTH2 with auto-refresh (token file), app-password fallback. "
                   "Send is workspace-file-attachment aware; credentials never appear in logs.")
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["search", "read", "send", "download_attachments"]},
            "query": {"type": "string", "description": "IMAP SEARCH criteria, e.g. 'SINCE 01-Jan-2026 UNSEEN (SUBJECT \"공모전\")'"},
            "uids": {"type": "array", "items": {"type": "string"}},
            "to": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string", "description": "plain text (html only if config body_html:true)"},
            "attachments": {"type": "array", "items": {"type": "string"}, "description": "paths, must live under workspace"},
            "mailbox": {"type": "string", "description": "default INBOX"},
            "thought": {"type": "string"},
        },
        "required": ["action", "thought"],
    }

    def __init__(self, workspace_root: str, cfg: Optional[Dict] = None):
        e = (cfg or {}).get("email", {}) or {}
        self.workspace_root = Path(workspace_root)
        self.provider = str(e.get("provider", "gmail")).lower()
        prov = PROVIDERS.get(self.provider, {})
        self.email_addr = e.get("email_addr") or os.getenv("CORTEX_EMAIL_ADDR") or ""
        self.imap_host = e.get("imap_host") or prov.get("imap", ("imap.gmail.com",))[0]
        self.imap_port = int(e.get("imap_port") or prov.get("imap", (0, 993))[1] or 993)
        self.smtp_host = e.get("smtp_host") or prov.get("smtp", ("smtp.gmail.com",))[0]
        self.smtp_port = int(e.get("smtp_port") or prov.get("smtp", (0, 465))[1] or 465)
        self.token_uri = e.get("token_uri") or prov.get("token_uri", "")
        self.token_file = Path(str(e.get("token_file", "~/.cortex/email_token.json"))).expanduser()
        self.access_token_env = e.get("access_token_env", "CORTEX_EMAIL_ACCESS_TOKEN")
        self.body_html = bool(e.get("body_html", False))
        self.max_fetch = int(e.get("max_fetch", 20))
        self.out_dir = self.workspace_root / "downloads" / "email"

    # ------------------------------------------------------------------ entry

    def execute(self, action: str = "", thought: str = "", **kw) -> ToolResult:
        if action not in ("search", "read", "send", "download_attachments"):
            return ToolResult(False, error=f"email: unknown action {action!r}")
        if (not self.email_addr or "@" not in self.email_addr
                or re.search(r"example\.(com|org)|your_email", self.email_addr, re.IGNORECASE)):
            return ToolResult(False, retryable=False,
                              error="email not configured: set secretary.email.email_addr + credentials "
                                    "(env CORTEX_EMAIL_ADDR / token file). Placeholder config never sends.")
        try:
            if action == "search":
                return self._search(kw.get("query", "ALL"), kw.get("mailbox", "INBOX"))
            if action == "read":
                return self._read(kw.get("uids") or [], kw.get("mailbox", "INBOX"))
            if action == "download_attachments":
                return self._download(kw.get("uids") or [], kw.get("mailbox", "INBOX"))
            return self._send(kw.get("to", ""), kw.get("subject", ""), kw.get("body", ""),
                              kw.get("attachments") or [])
        except Exception as e:
            return ToolResult(False, error=f"email {action}: {_redact(str(e))[:400]}", retryable=False)

    # -------------------------------------------------------------- auth layer

    def _access_token(self) -> Optional[str]:
        tok = os.getenv(self.access_token_env)
        if tok:
            return tok.strip()
        if self.token_file.exists():
            try:
                data = json.loads(self.token_file.read_text(encoding="utf-8"))
            except Exception:
                return None
            fresh_until = float(data.get("expires_at", 0))
            if data.get("access_token") and fresh_until > time.time() + 60:
                return data["access_token"]
            refreshed = self._refresh_token(data)
            if refreshed:
                data.update(refreshed)
                self.token_file.write_text(json.dumps(data), encoding="utf-8")
                return data["access_token"]
        return None

    def _refresh_token(self, data: Dict) -> Optional[Dict]:
        rt, cid = data.get("refresh_token"), data.get("client_id")
        if not (rt and cid and self.token_uri):
            return None
        form = {"client_id": cid, "refresh_token": rt, "grant_type": "refresh_token"}
        if data.get("client_secret"):
            form["client_secret"] = data["client_secret"]
        if data.get("scope"):
            form["scope"] = data["scope"]
        req = urllib.request.Request(self.token_uri,
                                     data=urllib.parse.urlencode(form).encode(),
                                     headers={"User-Agent": "cortex-secretary/1"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                j = json.loads(r.read().decode())
            return {"access_token": j["access_token"], "expires_at": time.time() + int(j.get("expires_in", 3600))}
        except Exception:
            return None

    def _password(self) -> str:
        return os.getenv("EMAIL_APP_PASSWORD") or ""     # config placeholder intentionally NOT used as secret

    def _imap_auth(self, imap):
        tok = self._access_token()
        if tok:
            imap.authenticate("XOAUTH2", lambda _: base64.b64encode(build_xoauth2(self.email_addr, tok)))
            return "oauth2"
        pw = self._password()
        if not pw:
            raise RuntimeError("no credentials: set " + self.access_token_env + " or EMAIL_APP_PASSWORD")
        imap.login(self.email_addr, pw)
        return "app-password"

    def _imap(self, mailbox: str = "INBOX"):
        import imaplib
        imap = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        self._imap_auth(imap)
        imap.select(mailbox)
        return imap

    # ---------------------------------------------------------------- actions

    def _search(self, query: str, mailbox: str) -> ToolResult:
        if not re.fullmatch(r"[A-Za-z0-9 _\"'()\\.+:\-,\u3131-\uD79A=]+", query or "ALL"):
            return ToolResult(False, error="email search: query contains disallowed characters",
                              retryable=False)
        imap = self._imap(mailbox)
        try:
            typ, data = imap.uid("SEARCH", None, query or "ALL")
            if typ != "OK":
                return ToolResult(False, error=f"search failed: {typ}")
            uids = [u.decode() for u in (data[0].split() if data and data[0] else [])][-self.max_fetch:]
            return ToolResult(True, data=uids, summary=f"{len(uids)} matching message(s) in {mailbox}")
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    def _read(self, uids: List[str], mailbox: str, want_attachments=False) -> List[Dict]:
        import email as email_lib
        imap = self._imap(mailbox)
        out = []
        try:
            for uid in uids[:self.max_fetch]:
                typ, msg_data = imap.uid("FETCH", uid, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                msg = email_lib.message_from_bytes(msg_data[0][1])
                text = html = ""
                atts = []
                for part in (msg.walk() if msg.is_multipart() else [msg]):
                    disp = str(part.get("Content-Disposition") or "")
                    ctype = part.get_content_type()
                    if "attachment" in disp:
                        raw = part.get_payload(decode=True) or b""
                        if want_attachments:
                            fname = self._safe_name(self._dh(part.get_filename()) or f"{uid}.bin")
                            if fname.rsplit(".", 1)[-1].lower() in ATTACH_EXT_WHITELIST:
                                self.out_dir.mkdir(parents=True, exist_ok=True)
                                (self.out_dir / fname).write_bytes(raw)
                        atts.append({"filename": self._dh(part.get_filename()) or "(unnamed)",
                                      "bytes": len(raw), "saved": bool(want_attachments)})
                    elif ctype == "text/plain" and not text:
                        text = (part.get_payload(decode=True) or b"").decode(errors="ignore")
                    elif ctype == "text/html" and not html:
                        html = (part.get_payload(decode=True) or b"").decode(errors="ignore")
                out.append({"uid": uid, "from": self._dh(msg["From"]), "to": self._dh(msg["To"]),
                            "subject": self._dh(msg["Subject"]), "date": str(msg["Date"]),
                            "text": text[:5000], "html": html[:3000], "attachments": atts})
        finally:
            try:
                imap.logout()
            except Exception:
                pass
        return out

    def _download(self, uids: List[str], mailbox: str) -> ToolResult:
        if not uids:
            return ToolResult(False, error="download_attachments needs 'uids'")
        saved = self._read(uids, mailbox, want_attachments=True)
        n = sum(1 for m in saved for a in m["attachments"] if a["saved"])
        return ToolResult(True, data=[m["uid"] for m in saved],
                          summary=f"{n} attachment(s) -> downloads/email/ (whitelist enforced)")

    def _send(self, to: str, subject: str, body: str, attachments: List[str]) -> ToolResult:
        import smtplib
        if not re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}", to or ""):
            return ToolResult(False, error=f"email send: invalid recipient {to!r}", retryable=False)
        msg = MIMEMultipart()
        msg["From"] = formataddr(("Cortex Secretary", self.email_addr))
        msg["To"] = to
        msg["Subject"] = str(make_header([(subject or "(no subject)", None)]))
        msg.attach(MIMEText(body or "", "html" if self.body_html else "plain", "utf-8"))
        for a in attachments:
            p = Path(str(a)).expanduser()
            if not p.is_absolute():
                p = self.workspace_root / p
            rp = p.resolve()
            if not str(rp).startswith(str(self.workspace_root.resolve())):
                return ToolResult(False, error=f"attachment outside workspace refused: {a}", retryable=False)
            if not rp.is_file():
                return ToolResult(False, error=f"attachment not found: {a}", retryable=False)
            if rp.name.rsplit(".", 1)[-1].lower() not in ATTACH_EXT_WHITELIST:
                return ToolResult(False, error=f"attachment type refused: {rp.suffix}", retryable=False)
            part = MIMEApplication(rp.read_bytes(), Name=rp.name)
            part.add_header("Content-Disposition", "attachment", filename=rp.name)
            msg.attach(part)

        tok, pw = self._access_token(), self._password()
        smtp = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port) if self.smtp_port == 465 \
            else smtplib.SMTP(self.smtp_host, self.smtp_port)
        try:
            if self.smtp_port != 465:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
            if tok:
                smtp.docmd("AUTH", "XOAUTH2 " +
                           base64.b64encode(build_xoauth2(self.email_addr, tok)).decode())
                r = smtp.getresp() if smtp.socket else ""
                if str(r).startswith("535"):
                    raise RuntimeError("SMTP XOAUTH2 rejected")
            elif pw:
                smtp.login(self.email_addr, pw)
            else:
                raise RuntimeError("no credentials for SMTP")
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:
                pass
        return ToolResult(True, summary=f"sent to {to}: {subject[:80]}")

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _dh(value) -> str:
        if not value:
            return ""
        try:
            return str(make_header(decode_header(value)))
        except Exception:
            return str(value)

    @staticmethod
    def _safe_name(name: str) -> str:
        n = Path(str(name)).name
        return re.sub(r"[^\w.\-\u3131-\uD79A ()]", "_", n)[:120] or "file.bin"
