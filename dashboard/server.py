"""Phase 5.2 - Cortex Cloud (web dashboard), stdlib-only.

    python -m dashboard.server [--port 8321] [--root <cortex_root>]

Endpoints
  GET /                    single-file UI (session timeline + live event stream)
  GET /api/sessions        meta list (newest first)
  GET /api/sessions/<id>   full event jsonl
  GET /api/stream[?session=ID]   SSE live tail (default: newest session)
  GET /api/stats           aggregate cost/token dashboard from config pricing

File-tailing (not sockets) so any agent process - CLI, GitHub bot, sandbox -
streams without sharing memory. SSE = stdlib, no extra deps.
"""
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _sessions_dir() -> Path:
    return ROOT / ".cortex_memory" / "sessions"


def _pricing() -> dict:
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
        return (cfg.get("dashboard") or {}).get("pricing", {"gpt-4o-mini": {"prompt": 0.15, "completion": 0.60}})
    except Exception:
        return {}


def list_sessions():
    from core.runtime.events import SessionLog
    return SessionLog.list_sessions(ROOT)


def read_events(run_id: str):
    f = _sessions_dir() / f"{run_id}.jsonl"
    if not f.exists() or ".." in run_id or "/" in run_id:
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def stats():
    metas = list_sessions()
    runs = len(metas)
    ok = sum(1 for m in metas if m.get("success"))
    tin = sum(m.get("est_prompt_tokens", 0) for m in metas)
    tout = sum(m.get("est_completion_tokens", 0) for m in metas)
    cost = 0.0
    price = _pricing()
    for m in metas:
        p = price.get(m.get("model", ""), {})
        cost += m.get("est_prompt_tokens", 0) / 1e6 * p.get("prompt", 0) \
              + m.get("est_completion_tokens", 0) / 1e6 * p.get("completion", 0)
    tools: dict = {}
    for ev in read_events(metas[0]["run_id"]) if metas else []:
        if ev["kind"] == "tool_call":
            tools[ev["data"]["tool"]] = tools.get(ev["data"]["tool"], 0) + 1
    return {"runs": runs, "successful": ok, "success_rate": round(ok / runs, 3) if runs else None,
            "est_prompt_tokens": tin, "est_completion_tokens": tout,
            "est_cost_usd": round(cost, 6), "last_run_tools": tools, "pricing": price}


HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Cortex Cloud</title>
<style>
 body{font:13px/1.45 ui-monospace,Menlo,monospace;background:#0d1117;color:#c9d1d9;margin:0;display:flex;height:100vh}
 #side{width:360px;border-right:1px solid #21262d;overflow:auto;padding:10px;flex-shrink:0}
 #main{flex:1;overflow:auto;padding:10px}
 .card{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:8px 10px;margin-bottom:8px;cursor:pointer}
 .card.sel{outline:1px solid #58a6ff}.ok{color:#3fb950}.bad{color:#f85149}.dim{color:#8b949e}
 .ev{border-left:2px solid #30363d;padding:3px 8px;margin:4px 0;background:#161b22;border-radius:0 6px 6px 0;white-space:pre-wrap}
 .ev.tool_call{border-left-color:#d29922}.ev.tool_result{border-left-color:#58a6ff}
 .ev.patch{border-left-color:#a371f7}.ev.done{border-left-color:#3fb950}.ev.test{border-left-color:#f85149}
 h3{margin:6px 0;color:#58a6ff;font-size:12px;text-transform:uppercase}
 #stats{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:8px}
 #stats div{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:6px;text-align:center}
 #stats b{font-size:16px;display:block}
</style></head><body>
<div id="side"><h3>Sessions</h3><div id="list"></div></div>
<div id="main"><h3>Cost &amp; tokens</h3><div id="stats"></div>
<h3>Live stream <span class="dim" id="livesid"></span></h3><div id="evs"></div></div>
<script>
const $=s=>document.querySelector(s);let cur=null;
async function j(u){const r=await fetch(u);return r.json()}
function esc(x){return (typeof x==='string')?x:JSON.stringify(x)}
async function reload(){
 const ss=await j('/api/sessions'),st=await j('/api/stats');
 $('#list').innerHTML=ss.map(s=>`<div class="card ${s.run_id===cur?'sel':''}" onclick="openS('${s.run_id}')">
  <b class="${s.success?'ok':'bad'}">${s.success?'✔':'✖'} ${esc(s.task||s.run_id)}</b><br>
  <span class="dim">${s.started} · ${s.iterations||'?'} iters · ${s.duration_s||'?'}s · ~${s.est_prompt_tokens||0}+${s.est_completion_tokens||0} tok</span></div>`).join('')||'<div class="dim">no sessions yet</div>';
 $('#stats').innerHTML=[['Runs',st.runs],['Success',st.success_rate==null?'-':(st.success_rate*100).toFixed(0)+'%'],
  ['Est. cost $',st.est_cost_usd],['Prompt tok',st.est_prompt_tokens],['Compl. tok',st.est_completion_tokens],
  ['Tools(last)',Object.entries(st.last_run_tools).map(([k,v])=>k+':'+v).join(' ')||'-']]
  .map(([k,v])=>`<div><b>${v}</b><span class="dim">${k}</span></div>`).join('');
}
function evHtml(e){const d=e.data||{};let s=`[${e.kind}] ${d.tool?d.tool+' ':''}${d.success===true?'OK':d.success===false?'FAIL':''}`;
 s+=d.summary?'\\n'+d.summary.slice(0,300):''; if(d.files)s+='\\nfiles: '+d.files.join(', ');
 if(d.phase)s+=` ${d.phase}: ${d.passed}p/${d.failed}f`; if(d.thought)s=`💭 ${d.thought}\\n`+s;
 if(d.task)s='🚀 '+d.task; if(e.kind==='done')s=`${d.success?'SUCCESS':'FAILED'} in ${d.iterations} iterations`;
 return `<div class="ev ${e.kind}"><span class="dim">${e.ts.slice(11)}</span> ${esc(s).replace(/&quot;/g,'"').replace(/\\\\n/g,'\\n')}</div>`}
async function openS(id){cur=id;const evs=await j('/api/sessions/'+id);$('#evs').innerHTML=evs.map(evHtml).join('');$('#livesid').textContent='(history: '+id+')';reload()}
let es;
function live(){if(es)es.close();es=new EventSource('/api/stream');es.onmessage=m=>{const e=JSON.parse(m.data);$('#livesid').textContent='(live: '+e.run_id+')';$('#evs').insertAdjacentHTML('beforeend',evHtml(e));$('#main').scrollTop=1e9;}}
reload();live();setInterval(reload,5000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("access-control-allow-origin", "*")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, HTML, "text/html; charset=utf-8")
        elif u.path == "/api/sessions":
            self._send(200, json.dumps(list_sessions(), ensure_ascii=False))
        elif u.path.startswith("/api/sessions/"):
            self._send(200, json.dumps(read_events(u.path.split("/")[-1]), ensure_ascii=False))
        elif u.path == "/api/stats":
            self._send(200, json.dumps(stats(), ensure_ascii=False))
        elif u.path == "/api/stream":
            q = parse_qs(u.query)
            sid = (q.get("session") or [""])[0]
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.end_headers()
            try:
                offset = 0
                target = None
                idle = 0
                while True:
                    if target is None:
                        metas = list_sessions()
                        target = (sid or (metas[0]["run_id"] if metas else None))
                        if target is None:
                            self.wfile.write(b"data: {\"kind\":\"idle\"}\n\n")
                            self.wfile.flush()
                            time.sleep(2)
                            continue
                    f = _sessions_dir() / f"{target}.jsonl"
                    if f.exists():
                        lines = f.read_text(encoding="utf-8").splitlines()
                        for ln in lines[offset:]:
                            try:
                                json.loads(ln)
                                self.wfile.write(f"data: {ln.strip()}\n\n".encode())
                            except Exception:
                                continue
                        if len(lines) > offset:
                            offset = len(lines)
                            idle = 0
                            self.wfile.flush()
                    done = (f.parent / f"{target}.meta.json").exists()
                    if done and offset >= len(read_events(target)):
                        self.wfile.write(f"data: {{\"kind\":\"stream_end\",\"run_id\":\"{target}\"}}\n\n".encode())
                        self.wfile.flush()
                        target = None  # re-pick newest for continuous live view
                    idle += 1
                    if idle % 15 == 0:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    time.sleep(0.7)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser(description="Cortex Cloud dashboard (stdlib, SSE)")
    ap.add_argument("--port", type=int, default=8321)
    ap.add_argument("--host", default="0.0.0.0")
    a = ap.parse_args()
    print(f"[DASHBOARD] http://{a.host}:{a.port} (root={ROOT})")
    ThreadingHTTPServer((a.host, a.port), H).serve_forever()


if __name__ == "__main__":
    main()
