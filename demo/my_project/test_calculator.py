import pytest
from calculator import divide, add
def test_add(): assert add(2, 3) == 5
def test_divide_normal(): assert divide(10, 2) == 5.0
def test_divide_by_zero(): assert divide(10, 0) is None
