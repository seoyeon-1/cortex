"""Phase 7.3 - research/: papers-with-code crawler + tracker glue + LaTeX generator.

`crawler.py` hits the arXiv API when reachable, else falls back to the bundled fixture
(research/fixtures/papers.json) so the pipeline always runs offline. `tracker.py` is a
wandb/mlflow-shaped dry-run client (jsonl). `latex.py` renders a tech-report draft from the
result JSON. Full AI-scientist loop wiring stays opt-in per config `research:`.
"""
