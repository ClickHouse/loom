"""Keys derived from a memory's text: `import:` and 24 hex characters of its SHA-256.

memory.import stores each item under such a key, so the same text always lands on the
same key. The key is a digest, not words about the item, so the excerpt and key-axis
text a row is embedded by leave it out (`build_excerpt`, `key_axis_text`).
"""

from __future__ import annotations

import re
from typing import Final

from server.core.hashing import sha256_text

IMPORT_KEY_PREFIX: Final = "import:"
# The hex characters of the text's SHA-256 a key keeps (96 bits).
IMPORT_KEY_HEX: Final = 24
_DIGEST_KEY: Final = re.compile(f"{re.escape(IMPORT_KEY_PREFIX)}[0-9a-f]{{{IMPORT_KEY_HEX}}}")


def import_key(value: str) -> str:
    """The key an item with text `value` is stored under, hashing the text as given."""
    return IMPORT_KEY_PREFIX + sha256_text(value)[:IMPORT_KEY_HEX]


def is_import_key(memory_key: str) -> bool:
    """Whether `memory_key` is exactly what `import_key` derives, nothing before or after."""
    return _DIGEST_KEY.fullmatch(memory_key) is not None
