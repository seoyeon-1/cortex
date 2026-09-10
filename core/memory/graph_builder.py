"""Phase 2.1 - Code Knowledge Graph builder.

Parses a repository with tree-sitter and produces a networkx.MultiDiGraph:

  Node types : File, Class, Function, Import
  Edge types : DEFINES (file->symbol), CONTAINS (class->method),
               CALLS   (function->symbol, heuristic), 
               IMPORTS (file->file), INHERITS (class->class)

Serialization: JSON (workspace_graph.json) via save()/load().

CLI:
    python -m core.memory.graph_builder <repo_path> [--out workspace_graph.json]

Parsing errors never abort the build (tip): they are logged to `graph.meta['errors']`.
"""
import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import networkx as nx
from tree_sitter import Language, Parser

PY_EXT = {".py"}
JS_EXT = {".js", ".jsx", ".mjs"}
TS_EXT = {".ts", ".tsx"}


def _try_language(module_name: str, getter: str = "language") -> Optional[Language]:
    try:
        mod = __import__(module_name)
        return Language(getattr(mod, getter)())
    except Exception:
        return None


class CodeGraphBuilder:
    SUPPORTED_EXTS = PY_EXT | JS_EXT | TS_EXT

    def __init__(self):
        self.errors: List[str] = []
        self._parsers: Dict[str, Optional[Parser]] = {}
        py = _try_language("tree_sitter_python")
        if py is None:
            raise RuntimeError("tree-sitter-python grammar not installed")
        self._parsers["py"] = Parser(py)
        js = _try_language("tree_sitter_javascript")
        self._parsers["js"] = Parser(js) if js else None
        ts = _try_language("tree_sitter_typescript")
        if ts is None:
            try:
                import tree_sitter_typescript as tsm
                ts = Language(tsm.language_typescript())
            except Exception:
                ts = None
        self._parsers["ts"] = Parser(ts) if ts else None

    # ------------------------------------------------------------------ build
    def build(self, repo_path: str) -> nx.MultiDiGraph:
        repo = Path(repo_path).resolve()
        g = nx.MultiDiGraph()
        g.graph["meta"] = {"repo": str(repo), "errors": []}
        files = self._list_source_files(repo)
        for rel in files:
            g.add_node(f"file:{rel}", type="File", name=rel, path=rel)
        defs_by_simple: Dict[str, List[str]] = {}          # simple name -> [node ids]
        module_to_file: Dict[str, str] = {}                 # dotted module -> rel path
        func_calls: List[tuple] = []                        # (func_id, callee_name)
        class_extends: List[tuple] = []                     # (class_id, base_name)
        file_imports: List[tuple] = []                      # (rel, module_dotted)

        for rel in files:
            try:
                src = (repo / rel).read_bytes()
                kind = Path(rel).suffix.lstrip(".")
                parser = self._parsers.get("py" if kind == "py" else ("js" if kind in JS_EXT and Path(rel).suffix in JS_EXT else "ts"))
                if parser is None:
                    continue
                tree = parser.parse(src)
                if kind == "py":
                    self._extract_python(repo, rel, g, tree, defs_by_simple, module_to_file, func_calls, class_extends, file_imports)
                else:
                    self._extract_js_ts(rel, g, tree, defs_by_simple, func_calls, file_imports)
            except Exception as e:  # never abort the build
                self.errors.append(f"{rel}: {e}")

        # resolution passes
        for func_id, callee in func_calls:
            for target in defs_by_simple.get(callee, []):
                if target != func_id:
                    g.add_edge(func_id, target, key="CALLS", type="CALLS")
        for class_id, base in class_extends:
            for target in defs_by_simple.get(base, []):
                if g.nodes[target]["type"] == "Class" and target != class_id:
                    g.add_edge(class_id, target, key="INHERITS", type="INHERITS")
        for rel, module in file_imports:
            target_rel = module_to_file.get(module)
            if target_rel and f"file:{target_rel}" != f"file:{rel}":
                g.add_edge(f"file:{rel}", f"file:{target_rel}", key="IMPORTS", type="IMPORTS", module=module)
        g.graph["meta"]["errors"] = self.errors
        return g

    def _list_source_files(self, repo: Path) -> List[str]:
        files: List[str] = []
        try:
            out = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True, text=True, timeout=15)
            if out.returncode == 0:
                files = [f for f in out.stdout.splitlines() if Path(f).suffix in self.SUPPORTED_EXTS]
        except Exception:
            pass
        if not files:
            for p in repo.rglob("*"):
                if p.is_file() and p.suffix in self.SUPPORTED_EXTS and not any(
                        seg.startswith(".") or seg in {"node_modules", "__pycache__", "venv", ".venv"}
                        for seg in p.relative_to(repo).parts):
                    files.append(str(p.relative_to(repo)))
        return sorted(set(files))

    # -------------------------------------------------------------- python
    def _extract_python(self, repo, rel, g, tree, defs_by_simple, module_to_file, func_calls, class_extends, file_imports):
        gid = f"file:{rel}"
        module_to_file[rel[:-3].replace(os.sep, ".")] = rel
        module_to_file[rel[:-3].replace(os.sep, ".") + ".__init__"] = rel
        for cls in tree.root_node.children:
            if cls.type == "class_definition":
                name = self._text(cls.child_by_field_name("name"))
                cid = f"class:{rel}::{name}"
                g.add_node(cid, type="Class", name=name, path=rel, line=cls.start_point[0] + 1,
                           doc=self._py_docstring(cls))
                g.add_edge(gid, cid, key="DEFINES", type="DEFINES")
                defs_by_simple.setdefault(name, []).append(cid)
                body = cls.child_by_field_name("body")
                for arg in self._superclass_names(cls):
                    class_extends.append((cid, arg))
                for node in (body.children if body else []):
                    if node.type == "function_definition":
                        self._py_func(rel, node, g, defs_by_simple, func_calls, parent=cid)
            elif cls.type == "function_definition":
                self._py_func(rel, cls, g, defs_by_simple, func_calls, parent=gid)
            elif cls.type in ("import_statement", "import_from_statement"):
                for mod in self._py_import_modules(cls):
                    g.add_node(f"import:{rel}::{mod}", type="Import", name=mod, path=rel)
                    g.add_edge(gid, f"import:{rel}::{mod}", key="IMPORTS", type="IMPORTS", module=mod)
                    file_imports.append((rel, mod))

    def _py_func(self, rel, node, g, defs_by_simple, func_calls, parent):
        name = self._text(node.child_by_field_name("name"))
        fid = f"func:{rel}::{(parent.split('::',1)[1] + '.' if parent.startswith('class:') else '')}{name}"
        params = node.child_by_field_name("parameters")
        g.add_node(fid, type="Function", name=name, path=rel, line=node.start_point[0] + 1,
                   signature=self._text(node)[:200].splitlines()[0], doc=self._py_docstring(node))
        g.add_edge(parent, fid, key="CONTAINS" if parent.startswith("class:") else "DEFINES",
                   type="CONTAINS" if parent.startswith("class:") else "DEFINES")
        defs_by_simple.setdefault(name, []).append(fid)
        for call in self._walk(node):
            if call.type == "call":
                callee = call.child_by_field_name("function")
                if callee is not None:
                    nm = self._text(callee).split(".")[-1]
                    if nm:
                        func_calls.append((fid, nm))

    def _py_docstring(self, node):
        body = node.child_by_field_name("body")
        if body and body.children and body.children[0].type == "expression_statement" \
                and body.children[0].children and body.children[0].children[0].type in ("string",):
            s = body.children[0].children[0]
            return self._text(s).strip("\"'")[:600]
        return ""

    def _py_import_modules(self, node):
        mods = []
        if node.type == "import_statement":
            for c in self._walk(node):
                if c.type in ("dotted_name", "identifier"):
                    mods.append(self._text(c))
                    break
            if not mods:
                for c in self._walk(node):
                    if c.type == "aliased_import":
                        n = c.children[0]
                        mods.append(self._text(n))
                        break
        else:
            m = node.child_by_field_name("module_name")
            if m is not None:
                mods.append(self._text(m))
            else:
                for c in node.children:
                    if c.type == "relative_import":
                        mods.append(self._text(c))
                        break
        return [m for m in mods if m]

    def _superclass_names(self, cls):
        names = []
        for c in cls.children:
            if c.type == "argument_list":
                for a in c.children:
                    if a.type in ("identifier", "attribute", "subscript"):
                        names.append(self._text(a).split(".")[-1])
        return names

    # ------------------------------------------------------------- js / ts
    def _extract_js_ts(self, rel, g, tree, defs_by_simple, func_calls, file_imports):
        gid = f"file:{rel}"
        root = tree.root_node
        for node in root.children:
            t = node.type
            if t in ("function_declaration", "generator_function_declaration"):
                name = self._text(node.child_by_field_name("name")) or "<anon>"
                fid = f"func:{rel}::{name}"
                g.add_node(fid, type="Function", name=name, path=rel, line=node.start_point[0] + 1,
                           signature=self._text(node)[:200].splitlines()[0], doc="")
                g.add_edge(gid, fid, key="DEFINES", type="DEFINES")
                defs_by_simple.setdefault(name, []).append(fid)
                for call in self._walk(node):
                    if call.type == "call_expression":
                        fn = call.child_by_field_name("function")
                        if fn is not None:
                            func_calls.append((fid, self._text(fn).split(".")[-1]))
            elif t == "class_declaration":
                name = self._text(node.child_by_field_name("name")) or "<anon>"
                cid = f"class:{rel}::{name}"
                g.add_node(cid, type="Class", name=name, path=rel, line=node.start_point[0] + 1, doc="")
                g.add_edge(gid, cid, key="DEFINES", type="DEFINES")
                defs_by_simple.setdefault(name, []).append(cid)
            elif t in ("import_statement",):
                for c in self._walk(node):
                    if c.type in ("string", "string_fragment"):
                        mods = self._text(c).strip("\"'").replace(".js", "").replace(".ts", "")
                        file_imports.append((rel, mods.replace("/", ".")))
                        break

    # ---------------------------------------------------------------- utils
    @staticmethod
    def _text(node) -> str:
        try:
            return node.text.decode("utf-8", "replace")
        except Exception:
            return ""

    def _walk(self, node):
        stack = [node]
        while stack:
            n = stack.pop()
            yield n
            stack.extend(reversed(n.children))

    # ------------------------------------------------------------ save/load
    @staticmethod
    def save(graph: nx.MultiDiGraph, path: str) -> None:
        data = {
            "meta": {k: (str(v) if not isinstance(v, (dict, list, int, float, str, bool)) else v)
                     for k, v in dict(graph.graph.get("meta", {})).items()},
            "nodes": [{"id": n, **graph.nodes[n]} for n in graph.nodes],
            "edges": [{"source": u, "target": v, **d}
                      for u, v, d in graph.edges(data=True)],
        }
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    @staticmethod
    def load(path: str) -> nx.MultiDiGraph:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        g = nx.MultiDiGraph()
        for n in data["nodes"]:
            nid = n.pop("id")
            g.add_node(nid, **n)
        for e in data["edges"]:
            u, v = e.pop("source"), e.pop("target")
            etype = e.get("type", "EDGE")
            g.add_edge(u, v, key=etype, **e)
        g.graph["meta"] = data.get("meta", {})
        return g


def main():
    ap = argparse.ArgumentParser(description="Build Cortex code knowledge graph")
    ap.add_argument("repo_path", nargs="?", default=".", help="Repository to scan")
    ap.add_argument("--out", default="workspace_graph.json", help="Output JSON path")
    args = ap.parse_args()

    g = CodeGraphBuilder().build(args.repo_path)
    CodeGraphBuilder.save(g, args.out)

    from rich.console import Console
    c = Console()
    ntypes: Dict[str, int] = {}
    etypes: Dict[str, int] = {}
    for _, d in g.nodes(data=True):
        ntypes[d.get("type", "?")] = ntypes.get(d.get("type", "?"), 0) + 1
    for _, _, d in g.edges(data=True):
        etypes[d.get("type", "?")] = etypes.get(d.get("type", "?"), 0) + 1
    c.print(f"[bold green]Graph built:[/bold green] {g.number_of_nodes()} nodes, {g.number_of_edges()} edges -> {args.out}")
    c.print(f"  nodes: {ntypes}")
    c.print(f"  edges: {etypes}")
    errs = g.graph.get("meta", {}).get("errors", [])
    if errs:
        c.print(f"[yellow]parse errors (non-fatal): {len(errs)}[/yellow] -> {errs[:3]}")


if __name__ == "__main__":
    main()
