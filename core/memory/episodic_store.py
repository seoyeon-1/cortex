"""Phase 15.3 - Episodic store facade (few-shot learning API over the Phase 3 store).

Deliberately thin: ONE source of truth for episodes stays in core.memory.episodic
(LanceDB with JSONL fallback + deterministic HashEmbedder vectors, WAL-free). This
module adds the experience-learning API:
  save_episode(task, success, trajectory, lessons)   - store a worked episode
  retrieve_similar(task, top_k, success_only=True)   - nearest past episodes
  format_as_fewshot(episodes)                        - render as few-shot examples

Embeddings reuse the shared embedder (all-MiniLM-L6-v2 if present, else 384-d
HashEmbedder), so search works fully offline and is deterministic in CI.
"""
from typing import Any, Dict, List, Optional

from core.memory.episodic import EpisodeStore


class EpisodicStore:
    def __init__(self, cortex_root: str, embed_model: Any = None):
        # embed_model kept for API familiarity; the underlying store owns the vectorizer
        self._store = EpisodeStore(cortex_root)

    def save_episode(self, task: str, success: bool, trajectory: List[Dict], lessons: str,
                     plan: str = "", patches: Optional[List[str]] = None,
                     test_result: str = "", git_commit_hash: str = "") -> Dict:
        return self._store.record(task=task, plan=plan, patches=patches or [],
                                  test_result=test_result, reflection=lessons,
                                  git_commit_hash=git_commit_hash, success=bool(success),
                                  trajectory=trajectory)

    def retrieve_similar(self, current_task: str, top_k: int = 3,
                         success_only: bool = False) -> List[Dict]:
        return self._store.similarity_search(current_task, top_k=top_k, success_only=success_only)

    def format_as_fewshot(self, episodes: List[Dict]) -> str:
        return EpisodeStore.format_as_fewshot(episodes)

    def fewshot_for_task(self, current_task: str, top_k: int = 2) -> str:
        return self._store.fewshot_for(current_task, top_k=top_k)

    def count(self) -> int:
        return self._store.count()
