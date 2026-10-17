---
paths:
  - "bench/**"
  - "docs/benchmarks/**"
---

# Benchmarks

- No run nobody asked for: ingesting and judging spend provider money and hours. Offline harnesses and `--help` are free.
- Run harnesses as `PYTHONPATH=$PWD .venv/bin/python bench/<harness>.py` (or the `make bench-*` targets) so this checkout's code runs.
- A result counts only when the treatment demonstrably ran; a change ships on the paired lower bound ([methodology](../../docs/benchmarks/methodology.md#the-verdict)).
- `docs/benchmarks/results.md` names files by bench and sections by model, never commits or dates. Run outputs (`bench/matrix_runs/`, `MATRIX_RESULTS.md`, CSVs) stay out of commits.
- Procedure: the `loom-bench` skill.
