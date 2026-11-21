"""`python -m server.cli` — the same entry point as the `loom` console script."""

import sys

from server.cli import main

sys.exit(main())
