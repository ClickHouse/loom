"""`python -m loom login | logout | whoami`: sign in with only the SDK installed."""

import sys

from loom.credentials import main

sys.exit(main())
