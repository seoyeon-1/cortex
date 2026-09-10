import os
import tiktoken
from typing import List, Dict, Any
from core.tools.file_ops import ReadFileTool, ListFilesTool

class ContextBuilder:
    def __init__(self, workspace_root: str, max_tokens: int = 16000, model: str = "gpt-4o-mini",
                 retriever=None):
        self.workspace_root = workspace_root
        self.max_tokens = max_tokens
        self.retriever = retriever  # Phase 2: HybridRetriever (optional, back-compat when None)
        try:
            self.enc = tiktoken.encoding_for_model(model)
        except KeyError:
            self.enc = tiktoken.get_encoding("cl100k_base")
        except Exception:
            # No BPE file available (offline): fall back to a len/4 heuristic budget.
            self.enc = None

        self.read_tool = ReadFileTool(workspace_root)
        self.list_tool = ListFilesTool(workspace_root)

    def count_tokens(self, text: str) -> int:
        if self.enc is None:
            return max(1, len(text) // 4)
        return len(self.enc.encode(text))

    def build_initial_context(self, task: str, test_results: Dict, extra_context: str = "") -> List[Dict]:
        messages = [
            {"role": "system", "content": self._load_system_prompt()},
            {"role": "user", "content": f"## TASK\n{task}\n\n## INITIAL TEST FAILURES\n{self._format_test_results(test_results)}"}
        ]

        if extra_context:
            messages.append({"role": "user", "content": extra_context[:6000]})

        injected = set()
        for h in (self._retriever_hits(task)):
            f = h.get("file", "")
            if not f or f in injected:
                continue
            injected.add(f)
            messages.append({"role": "user",
                             "content": f"## RELEVANT CODE via search_code -> {f}::{h.get('symbol')} (score={h.get('score')}, via={','.join(h.get('via', []))})\n```python\n{h['snippet']}\n```"})

        relevant_files = [f for f in self._guess_relevant_files(test_results) if f not in injected]
        for f in relevant_files[:5]:
            res = self.read_tool.execute(f, f"Loading relevant file {f} for context")
            if res.success:
                content = res.data
                if self.count_tokens(content) > 3000:
                    content = content[:12000] + "\n... [TRUNCATED] ..."
                messages.append({"role": "user", "content": f"## FILE: {f}\n```python\n{content}\n```"})

        return self._trim_to_budget(messages)

    def _retriever_hits(self, task: str) -> List[Dict]:
        if self.retriever is None:
            return []
        try:
            return self.retriever.search(task, top_k=7)
        except Exception as e:
            print(f"[CONTEXT] retriever unavailable, falling back to trace-based files: {e}")
            return []

    def _guess_relevant_files(self, test_results: Dict) -> List[str]:
        files = set()
        for fail in test_results.get("failures", []):
            trace = fail.get("trace", "")
            for line in trace.split("\n"):
                if 'File "' in line and ".py" in line:
                    try:
                        parts = line.split('"')
                        if len(parts) > 1:
                            f = parts[1]
                            if os.path.isabs(f):
                                try:
                                    f = os.path.relpath(f, self.workspace_root)
                                except ValueError:
                                    pass
                            if os.path.exists(os.path.join(self.workspace_root, f)):
                                files.add(f)
                    except: pass
        return list(files)

    def _format_test_results(self, tr: Dict) -> str:
        return f"Passed: {tr.get('passed', 0)}, Failed: {tr.get('failed', 0)}, Errors: {tr.get('errors', 0)}\nFailures Detail:\n" + \
               "\n".join([f"- {f.get('test', 'Unknown')}: {f.get('trace', '')[:200]}" for f in tr.get('failures', [])])

    def _load_system_prompt(self) -> str:
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        prompt_path = os.path.join(base_dir, "prompts", "system.md")
        if os.path.exists(prompt_path):
            with open(prompt_path, "r", encoding="utf-8") as f:
                return f.read()
        return "You are Cortex. Fix the code."

    def _trim_to_budget(self, messages: List[Dict]) -> List[Dict]:
        system_msg = messages[0]
        other_msgs = messages[1:]

        sys_tokens = self.count_tokens(system_msg["content"])
        current_tokens = sys_tokens + sum(self.count_tokens(m["content"]) for m in other_msgs)

        if current_tokens <= self.max_tokens:
            return messages

        while other_msgs and (sys_tokens + sum(self.count_tokens(m["content"]) for m in other_msgs)) > self.max_tokens:
            other_msgs.pop(0)
        return [system_msg] + other_msgs
