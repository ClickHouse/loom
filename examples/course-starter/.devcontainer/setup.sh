#!/bin/sh
# Sets up the codespace when it is created: the labs' .venv first, then the Loom
# stack and Claude Code. start-loom.sh is a copy of the Loom repository's codespace
# script, as it was when this course was copied.
set -eu

sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv
python3 -m venv .venv
.venv/bin/pip install --quiet -r requirements.txt

sh .devcontainer/start-loom.sh

cat <<'TEXT'

The try-out above is Loom's own; the labs are in README.md. Run `. .venv/bin/activate`
in each new terminal, then `python grading/grade.py` to check your work.
TEXT
