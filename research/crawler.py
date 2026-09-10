"""Phase 7.3a - papers crawler: arXiv API (live) with offline fixture fallback."""
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "papers.json"


def fetch(query: str, max_results: int = 5, timeout: float = 8.0) -> Dict:
    """returns {"source": "arxiv"|"fixture", "papers": [{id,title,abstract,url,code_hint}]}"""
    try:
        q = urllib.parse.urlencode({"search_query": f"all:{query.replace(' ', '+')}",
                                    "start": 0, "max_results": max_results})
        raw = urllib.request.urlopen(f"http://export.arxiv.org/api/query?{q}", timeout=timeout).read().decode()
        ns = {"a": "http://www.w3.org/2005/Atom"}
        papers = []
        for e in ET.fromstring(raw).findall("a:entry", ns):
            title = re.sub(r"\s+", " ", e.findtext("a:title", "", ns)).strip()
            absr = re.sub(r"\s+", " ", e.findtext("a:summary", "", ns)).strip()
            link = e.findtext("a:id", "", ns)
            papers.append({"id": link.rsplit("/", 1)[-1], "title": title, "abstract": absr[:600],
                           "url": link, "code_hint": "github" if "github.com" in absr else ""})
        if papers:
            return {"source": "arxiv", "query": query, "papers": papers}
        raise RuntimeError("empty feed")
    except Exception as ex:
        papers = json.loads(FIXTURE.read_text()) if FIXTURE.exists() else []
        return {"source": "fixture", "query": query, "error": str(ex)[:120], "papers": papers[:max_results]}


def hypotheses(papers: List[Dict]) -> List[str]:
    """crude but real: extract imperative claims from abstracts -> candidate experiment lines."""
    out = []
    for p in papers:
        for sent in re.split(r"(?<=[.!?])\s+", p.get("abstract", "")):
            if re.search(r"\b(improv|reduc|outperform|faster|robust|scale)\w*\b", sent, re.I) and 30 < len(sent) < 220:
                out.append(f"[{p['id']}] {sent.strip()}")
                break
    return out[:6]
