import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';

interface BrowserState {
  url?: string; title?: string; action?: string; ok?: boolean;
  error?: string; ts?: number; preview?: string | null;
}

/**
 * CortexChatViewProvider - sidebar webview combining:
 *  - live browser preview: polls <workspace>/.cortex_browser/state.json + preview.png
 *    (written after EVERY BrowserTool action - no IPC needed between python and the IDE)
 *  - one-click task launch against the current folder
 *  - last episodes/directives are already streamed by the dashboard, so we deep-link it
 */
export class CortexChatViewProvider implements vscode.WebviewViewProvider {
  public static readonly viewId = 'cortex.chat';
  private view?: vscode.WebviewView;
  private timer?: NodeJS.Timeout;
  private watcher?: fs.FSWatcher;
  private lastTs = 0;
  private root: string;

  constructor(private readonly ctx: vscode.ExtensionContext) {
    this.root = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? process.cwd();
  }

  resolveWebviewView(webviewView: vscode.WebviewView) {
    this.view = webviewView;
    webviewView.webview.options = { enableScripts: true, localResourceRoots: [vscode.Uri.file(this.root)] };
    webviewView.webview.html = this.html();

    webviewView.webview.onDidReceiveMessage(msg => {
      if (msg?.command === 'runTask') {
        vscode.commands.executeCommand('cortex.runTask');
      } else if (msg?.command === 'openDashboard') {
        vscode.env.openExternal(vscode.Uri.parse('http://127.0.0.1:8321'));
      } else if (msg?.command === 'refresh') {
        this.poll(true);
      }
    });

    const stateFile = path.join(this.root, '.cortex_browser', 'state.json');
    try { this.watcher = fs.watch(path.dirname(stateFile), () => this.poll()); } catch { /* dir may appear later */ }
    this.timer = setInterval(() => this.poll(), 900);          // cheap mtime-free fallback poll
    this.poll(true);
  }

  dispose() { if (this.timer) { clearInterval(this.timer); } this.watcher?.close(); }

  private poll(force = false) {
    if (!this.view) return;
    const dir = path.join(this.root, '.cortex_browser');
    let state: BrowserState | null = null;
    try {
      state = JSON.parse(fs.readFileSync(path.join(dir, 'state.json'), 'utf8'));
    } catch { state = null; }
    const ts = state?.ts ?? 0;
    if (!force && ts === this.lastTs) return;                  // nothing new
    this.lastTs = ts;
    let img = '';
    if (state?.preview) {
      try {
        const png = path.join(dir, state.preview);
        if (fs.existsSync(png)) {
          const uri = this.view.webview.asWebviewUri(vscode.Uri.file(png));
          img = `<img src="${uri.toString(true)}?v=${ts}" alt="browser preview"/>`;
        }
      } catch { /* torn write - next poll retries */ }
    }
    this.view.webview.postMessage({
      type: 'preview',
      ok: !!state?.ok,
      head: state ? `${state.action ?? '?'} ${state.ok === false ? '✗' : '✓'}` : '대기 중',
      url: state?.url ?? '', title: state?.title ?? '', error: state?.error ?? '',
      when: state?.ts ? new Date(state.ts * 1000).toLocaleTimeString() : '',
      img,
      empty: state ? '' : '브라우저 액션이 실행되면 스크린샷이 여기에 표시됩니다. (BrowserTool → .cortex_browser/preview.png)',
    });
  }

  private html(): string {
    const csp = "default-src 'none'; img-src " + this.view?.webview.cspSource + " data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'";
    return `<!DOCTYPE html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${csp}">
<style>
  body { font-family: var(--vscode-font-family); font-size: 12px; color: var(--vscode-foreground); padding: 8px; }
  .card { border: 1px solid var(--vscode-panel-border); border-radius: 6px; padding: 8px; margin-bottom: 8px; }
  .row { display: flex; gap: 6px; align-items: center; margin-bottom: 4px; }
  .badge { padding: 1px 6px; border-radius: 8px; font-weight: 600; }
  .ok { background: var(--vscode-testing-iconPassed, #2a7); color: #fff; }
  .fail { background: var(--vscode-errorForeground, #c33); color: #fff; }
  .muted { opacity: .65; } .small { font-size: 10.5px; }
  img { width: 100%; border: 1px solid var(--vscode-panel-border); border-radius: 4px; margin-top: 6px; }
  button { background: var(--vscode-button-background); color: var(--vscode-button-foreground);
           border: 0; border-radius: 3px; padding: 4px 10px; cursor: pointer; }
  a { color: var(--vscode-textLink-foreground); }
  .err { color: var(--vscode-errorForeground); white-space: pre-wrap; }
</style></head><body>
  <div class="card">
    <div class="row"><b>Cortex · browser preview</b><span id="when" class="muted small"></span>
      <button id="refresh" style="margin-left:auto">↻</button></div>
    <div class="row"><span id="head" class="badge ok">대기</span><span id="title" class="muted"></span></div>
    <div id="url" class="small muted"></div>
    <div id="body"></div>
    <div id="err" class="err small"></div>
  </div>
  <div class="card">
    <div class="row"><button id="run">▶ Run task on this repo</button>
      <a href="#" id="dash" class="small">dashboard :8321</a></div>
    <div class="small muted">headful 브라우저에서 최종 제출을 직접 확인하세요 (fill_form은 기본 자동-제출 안 함).</div>
  </div>
<script>
  const acc = acquireVsCodeApi();
  const state = acc.getState() || { html: '' };
  const $ = (id) => document.getElementById(id);
  document.getElementById('body').innerHTML = state.html || '';
  if (state.head) render(state);
  function render(m) {
    $('head').textContent = m.head; $('head').className = 'badge ' + (m.ok ? 'ok' : 'fail');
    $('title').textContent = m.title || ''; $('when').textContent = m.when || '';
    $('url').textContent = m.url || ''; $('err').textContent = m.error || '';
    $('body').innerHTML = m.img || m.empty || '';
    acc.setState({ html: $('body').innerHTML, head: m.head });
  }
  window.addEventListener('message', e => { if (e.data?.type === 'preview') render(e.data); });
  $('refresh').onclick = () => acc.postMessage({ command: 'refresh' });
  $('run').onclick = () => acc.postMessage({ command: 'runTask' });
  $('dash').onclick = (e) => { e.preventDefault(); acc.postMessage({ command: 'openDashboard' }); };
</script></body></html>`;
  }
}
