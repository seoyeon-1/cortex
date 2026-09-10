## EDIT_FILE TOOL USAGE
- `unified_diff` argument must be a single string containing the full patch.
- Example:
  --- a/calculator.py
  +++ b/calculator.py
  @@ -1,3 +1,5 @@
   def divide(a, b):
  +    if b == 0:
  +        return None
       return a / b
- `thought` argument: "Added zero division guard to satisfy test_divide_by_zero requirement."
