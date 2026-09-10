## PLANNING GUIDELINES
- If test fails with `AssertionError`, check the expected vs actual values.
- If `ImportError` / `ModuleNotFoundError`, check imports and `sys.path`.
- If `SyntaxError` / `IndentationError`, the previous patch was bad. Read the file again and fix carefully.
- If `AttributeError` / `TypeError`, check function signatures and object types.
- Always prefer editing the *source* code, not the test code, unless the task says "fix the test".
