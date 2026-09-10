"""Phase 3.3 - Skill Library ("making tools" automation).

skills/*.yaml  : reusable recipe = prompt guidance + Jinja2 template + optional validate_cmd
Skills are injected as context when their `trigger` regex matches the task.

Self-improvement: SkillLearner watches successful patch patterns; when the SAME
pattern (same tool set + same file shape) succeeded >= threshold times it writes a
DRAFT skill under skills/candidates/ and proposes promotion ("이거 스킬로 등록할까?").
Human approves with: python -m core.memory.skills promote <slug>  -> moves to skills/.
"""
import hashlib
import re
from pathlib import Path
from typing import Dict, List, Optional

import yaml

_TEMPLATE = """# skills/<name>.yaml
name: my-skill
description: one-liner
trigger: "(?i)regex that matches a task"
prompt: |
  Instructions injected into the agent context when trigger matches.
template: |
  {{ var }} - Jinja2 starter content (optional)
validate_cmd: "pytest -q"
"""


class Skill:
    def __init__(self, path: Path, data: Dict):
        self.path = path
        self.name: str = data.get("name", path.stem)
        self.description: str = data.get("description", "")
        self.trigger: str = data.get("trigger", "")
        self.prompt: str = data.get("prompt", "")
        self.template: str = data.get("template", "")
        self.validate_cmd: str = data.get("validate_cmd", "")

    def matches(self, task: str) -> bool:
        return bool(self.trigger) and re.search(self.trigger, task, re.IGNORECASE) is not None

    def render_template(self, **ctx) -> str:
        if not self.template:
            return ""
        try:
            from jinja2 import Template
            return Template(self.template, keep_trailing_newline=True).render(**ctx)
        except Exception as e:
            return f"[template render error: {e}]"

    def as_context(self, **ctx) -> str:
        block = f"## ACTIVE SKILL: {self.name}\n{self.description}\n\n{self.prompt.strip()}"
        rendered = self.render_template(**ctx)
        if rendered.strip():
            block += f"\n\n### Starter template\n```\n{rendered.strip()}\n```"
        if self.validate_cmd:
            block += f"\n\n### Validation\nRun `{self.validate_cmd}` before declaring DONE."
        return block[:4000]


class SkillLibrary:
    def __init__(self, skills_dir: str | Path):
        self.dir = Path(skills_dir)
        self.candidates_dir = self.dir / "candidates"

    def load(self) -> List[Skill]:
        out: List[Skill] = []
        if not self.dir.exists():
            return out
        for f in sorted(self.dir.glob("*.yaml")) + sorted(self.dir.glob("*.yml")):
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                if isinstance(data, dict) and data.get("name"):
                    out.append(Skill(f, data))
            except Exception as e:
                print(f"[SKILLS] skipping {f.name}: {e}")
        return out

    def match(self, task: str) -> List[Skill]:
        return [s for s in self.load() if s.matches(task)]

    def context_for(self, task: str) -> str:
        skills = self.match(task)
        return "\n\n".join(s.as_context(task=task) for s in skills) if skills else ""


class SkillLearner:
    """Pattern counter + candidate generator (approval is human-gated)."""

    def __init__(self, cortex_root: str | Path, threshold: int = 3):
        self.dir = Path(cortex_root) / ".cortex_memory"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.stats_file = self.dir / "skill_stats.json"
        self.threshold = threshold
        self.library = SkillLibrary(Path(cortex_root) / "skills")

    @staticmethod
    def signature(task: str, tools: List[str], files: List[str]) -> str:
        """Coarse pattern key: which tools over which file-shape (names ignored, count+ext kept)."""
        shape = ",".join(sorted({Path(f).suffix or "noext" for f in files})) or "none"
        nfiles = str(len(set(files)))
        return f"tools:{'+'.join(sorted(set(t for t in tools if t)))}|files:{nfiles}:{shape}"

    def _load(self) -> Dict:
        try:
            return yaml.safe_load(self.stats_file.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}

    def observe_success(self, task: str, tools: List[str], files: List[str]) -> Optional[Dict]:
        """Call after a run whose patches applied and tests passed.
        Returns a proposal dict when the same pattern hit the threshold, else None."""
        sig = self.signature(task, tools, files)
        stats = self._load()
        entry = stats.setdefault(sig, {"count": 0, "examples": []})
        entry["count"] += 1
        entry["examples"] = (entry["examples"] + [task])[-5:]
        proposal = None
        if entry["count"] >= self.threshold:
            slug = "skill-" + hashlib.sha1(sig.encode()).hexdigest()[:8]
            cand_dir = self.library.candidates_dir
            draft = cand_dir / f"{slug}.yaml"
            if not draft.exists() and not (self.library.dir / f"{slug}.yaml").exists():
                cand_dir.mkdir(parents=True, exist_ok=True)
                kws = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", " ".join(entry["examples"]))
                top = [w.lower() for w in kws[:2]]
                trigger = "(?i)" + ".*".join(map(re.escape, top)) if top else "(?i)."
                draft.write_text(yaml.safe_dump({
                    "name": slug,
                    "description": f"Learned pattern (auto-draft): {sig}",
                    "trigger": trigger,
                    "prompt": "Reuse the approach from the examples below; keep diffs minimal and test-verified.\n\nExamples:\n" +
                              "\n".join(f"- {e[:110]}" for e in entry["examples"]),
                    "template": "",
                    "validate_cmd": "pytest -q",
                    "status": "candidate",
                }, allow_unicode=True, sort_keys=False), encoding="utf-8")
                proposal = {"slug": slug, "pattern": sig, "count": entry["count"], "draft": str(draft)}
        self.stats_file.write_text(yaml.safe_dump(stats, allow_unicode=True), encoding="utf-8")
        return proposal

    @staticmethod
    def promote(cortex_root: str | Path, slug: str) -> Optional[Path]:
        lib = SkillLibrary(Path(cortex_root) / "skills")
        src = lib.candidates_dir / f"{slug}.yaml"
        if not src.exists():
            return None
        lib.dir.mkdir(parents=True, exist_ok=True)
        data = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
        data["status"] = "active"
        dst = lib.dir / f"{slug}.yaml"
        dst.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        src.unlink()
        return dst


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Cortex skill library")
    ap.add_argument("cmd", choices=["list", "candidates", "promote", "template"])
    ap.add_argument("slug", nargs="?", default="")
    a = ap.parse_args()
    root = Path(__file__).resolve().parents[2]
    if a.cmd == "template":
        print(_TEMPLATE)
    elif a.cmd == "candidates":
        for f in sorted(SkillLibrary(root / "skills").candidates_dir.glob("*.yaml")) if SkillLibrary(root / "skills").candidates_dir.exists() else []:
            print(f.name, "|", yaml.safe_load(f.read_text())["description"][:80])
    elif a.cmd == "promote":
        p = SkillLearner.promote(root, a.slug)
        print(f"promoted -> {p}" if p else f"no candidate named '{a.slug}' under skills/candidates/")
    else:
        for s in SkillLibrary(root / "skills").load():
            print(f"- {s.name}: {s.description[:80]} [trigger={s.trigger[:40]}]")


if __name__ == "__main__":
    main()
