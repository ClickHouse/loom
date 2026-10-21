"""The shape of a memory chunk: how many characters `chunk_projection` cuts and how many
it repeats from the chunk before."""

from __future__ import annotations

# 800 chars ≈ 200 tokens; preserves multi-sentence units a 400-char window splits
# mid-thought.
CHUNK_SIZE = 800  # not a setting: algorithm shape, changed only with a benchmark
# The characters a chunk repeats from the one before it, so a boundary-straddling
# date or entity appears in both neighbours.
CHUNK_OVERLAP = 100  # not a setting: algorithm shape, changed only with a benchmark
