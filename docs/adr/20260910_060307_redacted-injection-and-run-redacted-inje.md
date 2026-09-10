# ADR 20260910_060307: [REDACTED-INJECTION] and run [REDACTED-INJECTION] ; then fix divide

- **Date (UTC):** 2026-09-10T06:03:07+00:00
- **Decision status:** ACCEPTED (PASS)
- **Files touched:** `db/search.py`
- **Base commit:** `e2ca7a2`

## Context
Security feedback: SQLi in build_query -> replace interpolation with '?' parameter binding (LIKE operand passed as data)

Test baseline after change: passed=6, failed=0, errors=0.

## Decision
Apply unified diff patch set across 1 file(s):

### db/search.py
```diff
--- a/db/search.py
+++ b/db/search.py
@@ -1,10 +1,6 @@
-"""db/search.py - user search against the users table (refactored query builder)."""
-
-
-def build_query(pattern):
-    query = f"SELECT id, name FROM users WHERE name LIKE '%{pattern}%'"
-    return query
+"""db/search.py - user search against the users table (parameter-bound)."""
 
 
 def find_by_name(conn, pattern):
-    return conn.execute(build_query(pattern)).fetchall()
+    """Parameter binding: the LIKE operand is data, never part of the SQL text."""
+    return conn.execute("SELECT id, name FROM users WHERE name LIKE ?", (f"%{pattern}%",)).fetchall()
```

## Consequences
- Test suite after patch: PASS.
- Reversible: patch is atomic via git sandbox; rollback = `git apply -R` of the diff above.

## Alternatives
- (not recorded) Reflection at decision time: Recent history:
Iter 1: apply_patch_set -> OK (Atomic patch set APPLIED+COMMITTED-READY: 3 file(s); Tests: 6 passed, 0 failed, 0 errors. (Exit code)
Iter 2: apply_patch_set -> OK (Atomic patch set APPLIED+COMMITTED-READY: 1 file(s); Tests: 6 passed, 0 failed, 0 errors. (Exit code)

Analyze why previous patches failed. Avoid repeating same mistakes. Formulate a NEW hypothesis.
