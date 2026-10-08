"""`python -m server.cli` — the same entry point as the `loom` console script."""

import sys

from server.cli.app import main

sys.exit(main())
