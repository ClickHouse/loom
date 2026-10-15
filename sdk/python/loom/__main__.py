"""`python -m loom login | logout | whoami`: sign in with only the SDK installed."""

import sys

from loom.credentials import main

if __name__ == "__main__":
    sys.exit(main())
