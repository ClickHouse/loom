"""Import the standalone plugin as Dify imports it from its package root."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
