from core.tools.base import BaseTool, ToolResult


class SearchCodeTool(BaseTool):
    name = "search_code"
    description = ("Hybrid codebase search: semantic vectors + AST knowledge graph (CALLS/IMPORTS/INHERITS) + symbol keywords, fused via RRF. "
                   "Use INSTEAD of read_file/list_files to locate files, symbols, callers and dependencies in one shot. "
                   "Returns ranked snippets: file, symbol, line, snippet, score.")
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural-language question or symbol/keyword (e.g., 'who calls UserService.get_user', 'payment retry logic')"},
            "top_k": {"type": "integer", "default": 7, "description": "Number of results"},
            "thought": {"type": "string", "description": "Why you are searching this now"}
        },
        "required": ["query", "thought"]
    }

    def __init__(self, workspace_root: str, retriever=None):
        self.workspace_root = workspace_root
        self._retriever = retriever

    def _get_retriever(self):
        if self._retriever is None:
            from core.memory.retriever import HybridRetriever
            self._retriever = HybridRetriever(self.workspace_root)
            self._retriever.index_codebase(self.workspace_root)
        return self._retriever

    def execute(self, query: str, thought: str, top_k: int = 7) -> ToolResult:
        try:
            hits = self._get_retriever().search(query, top_k=int(top_k or 7))
        except Exception as e:
            return ToolResult(False, error=f"search_code failed: {e}")
        lines = [f"{i + 1}. {h['file']}::{h['symbol']} [{h['node_type']}@L{h.get('line', 0)}] score={h['score']} via={','.join(h.get('via', []))}"
                 for i, h in enumerate(hits)]
        return ToolResult(True, data=hits,
                          summary=f"search_code '{query}' -> {len(hits)} hit(s)\n" + "\n".join(lines[:5]) if hits
                                  else f"search_code '{query}' -> no hits")
