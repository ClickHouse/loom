#!/bin/sh
# Sets up the codespace when it is created: the Loom stack, by the script the Loom
# repository's own codespace runs (fetched, so the two never differ), then the
# labs' Python environment in .venv.
set -eu

start=$(mktemp)
trap 'rm -f "$start"' EXIT
curl -fsSL --retry 3 https://raw.githubusercontent.com/ClickHouse/loom/main/.devcontainer/start-loom.sh -o "$start"
sh "$start"

sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv
python3 -m venv .venv
.venv/bin/pip install --quiet -r requirements.txt

cat <<'TEXT'

The labs are in README.md. Run `. .venv/bin/activate` in each new terminal, then
`python grading/grade.py` to check your work.
TEXT
