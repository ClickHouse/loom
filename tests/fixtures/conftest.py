"""Test inputs, not tests: the code-lane corpus holds source trees with their own
`test_*.py` files and `conftest.py`, which pytest must neither collect nor import."""

collect_ignore_glob = ["*"]
