"""Phase 16 - Secretary kit: stateful browser, OAuth2 email, competition intel, form filler.

All tools degrade GRACEFULLY (clear ToolResult errors, no crashes) when optional
dependencies (playwright) or credentials are absent, and are registered into the
AgentLoop/Orchestrator tool registry behind the `secretary:` config block.

Preview contract for the VS Code sidebar: after every browser action the tool writes
  <workspace>/.cortex_browser/preview.png   + <workspace>/.cortex_browser/state.json
which ChatViewProvider live-polls (no IPC needed).
"""
from core.tools.secretary.browser import BrowserTool  # noqa: F401
from core.tools.secretary.email_client import EmailTool  # noqa: F401
from core.tools.secretary.competition_intel import CompetitionIntelTool  # noqa: F401
from core.tools.secretary.form_filler import FormFillerTool  # noqa: F401

__all__ = ["BrowserTool", "EmailTool", "CompetitionIntelTool", "FormFillerTool"]
