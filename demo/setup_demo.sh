#!/usr/bin/env bash
# Recreate Cortex demo target repos next to the cortex checkout (idempotent).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; CORTEX="$(dirname "$HERE")"
gitcfg() { git -c user.name="${GIT_AUTHOR_NAME:-cortex-demo}" -c user.email="${GIT_AUTHOR_EMAIL:-demo@cortex.local}" "$@"; }
PHASE="${1:-}"   # "phase6" = green suite + legacy SQLi module + failing search test (agent-society demo)
for proj in my_project; do
  TARGET="$(dirname "$CORTEX")/$proj"
  [ -d "$TARGET/.git" ] && { echo "[setup] $TARGET exists - left untouched"; continue; }
  rm -rf "$TARGET"; mkdir -p "$TARGET"
  cp -R "$HERE/$proj/." "$TARGET/"
  (cd "$TARGET" && git init -q && git add -A && gitcfg commit -q -m "init")
  if [ "$PHASE" = "phase6" ]; then
    python3 - "$TARGET" << 'P6EOF'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
calc = root / "calculator.py"; t = calc.read_text()
t = t.replace("def divide(a, b):\n    return a / b", "def divide(a, b):\n    if b == 0:\n        return None\n    return a / b")
calc.write_text(t)
(root / "db").mkdir(exist_ok=True)
(root / "db" / "search.py").write_text('''"""db/search.py - LEGACY user search against the users table.

Written years ago: the SQL is built by string interpolation. Refactor so callers
use parameter binding - no interpolation of user input into SQL."""
import sqlite3


def find_by_name(conn, pattern):
    query = "SELECT id, name FROM users WHERE name LIKE '%" + pattern + "%'"
    return conn.execute(query).fetchall()
''')
(root / "tests" / "test_search.py").write_text('''import sqlite3

from repo.user_repo import UserRepo
from service.user_service import UserService


def _conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE users (id TEXT, name TEXT)")
    c.executemany("INSERT INTO users VALUES (?, ?)", [("1", "alice"), ("2", "bob")])
    return c


def test_service_search_finds_alice():
    repo = UserRepo(conn=_conn())
    svc = UserService(repo=repo)
    assert [r[1] for r in svc.search("al")] == ["alice"]
''')
P6EOF
    (cd "$TARGET" && git add -A && gitcfg commit -q -m "phase6 baseline: safe calculator + legacy SQLi db/search.py + failing search test")
    echo "[setup] $TARGET ready (PHASE6: search test failing, legacy SQLi module present)"
  else
    echo "[setup] $TARGET ready (1 failing test: test_divide_by_zero)"
  fi
done
