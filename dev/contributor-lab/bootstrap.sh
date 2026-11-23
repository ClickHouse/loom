#!/bin/bash
set -euo pipefail

uv sync --locked --extra dev --extra code
uv run --no-sync python dev/contributor-lab/test_lab.py
uv run --no-sync python dev/contributor-lab/check.py "${1:-functional}"
