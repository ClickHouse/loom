"""`python -m loom login | logout | whoami | call`: with only the SDK installed."""

import sys

from loom.cli import main

if __name__ == "__main__":
    sys.exit(main())
