import * as vscode from 'vscode';
import * as cp from 'child_process';
import { LanguageClient, LanguageClientOptions, ServerOptions, TransportKind } from 'vscode-languageclient/node';
import { CortexChatViewProvider } from './chatView';

let client: LanguageClient | undefined;

export function activate(ctx: vscode.ExtensionContext) {
  const root = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? process.cwd();

  // Cortex-aware LSP (documentSymbol / workspace/symbol served by the code knowledge graph)
  const server: ServerOptions = {
    run:   { command: 'python', args: ['-m', 'core.lsp.server', '--root', root],
             options: { cwd: process.env.CORTEX_HOME ?? root, env: { ...process.env, PYTHONPATH: process.env.CORTEX_HOME ?? root } } },
    debug: { command: 'python', args: ['-m', 'core.lsp.server', '--root', root],
             options: { cwd: process.env.CORTEX_HOME ?? root } }
  };
  const opts: LanguageClientOptions = { documentSelector: [{ scheme: 'file', language: 'python' }] };
  client = new LanguageClient('cortexLsp', 'Cortex LSP', server, opts);
  client.start();

  // Phase 16: sidebar chat/preview webview (live browser screenshots from .cortex_browser/)
  const chat = new CortexChatViewProvider(ctx);
  ctx.subscriptions.push(vscode.window.registerWebviewViewProvider(CortexChatViewProvider.viewId, chat));
  ctx.subscriptions.push(vscode.commands.registerCommand('cortex.focusChat', async () => {
    await vscode.commands.executeCommand('cortex.chat.focus');
  }));

  ctx.subscriptions.push(
    vscode.commands.registerCommand('cortex.runTask', async () => {
      const task = await vscode.window.showInputBox({ prompt: 'Cortex task', placeHolder: 'e.g. make failing tests pass' });
      if (!task) return;
      const term = vscode.window.createTerminal('Cortex');
      term.show();
      term.sendText(`cd "${process.env.CORTEX_HOME ?? 'cortex'}" && python main.py "${task.replace(/"/g, '\\"')}" --repo "${root}" --yes`);
    }),
    vscode.commands.registerCommand('cortex.openDashboard', async () => {
      const child = cp.spawn('python', ['-m', 'dashboard.server', '--port', '8321'],
        { cwd: process.env.CORTEX_HOME ?? 'cortex', detached: true, stdio: 'ignore' });
      child.unref();
      vscode.env.openExternal(vscode.Uri.parse('http://127.0.0.1:8321'));
    })
  );
}
export function deactivate() { return client?.stop(); }

// helper for the chat webview: reuse the dashboard command when posted

