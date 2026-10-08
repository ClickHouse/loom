"""`prefix:<dim>:<model>` labels: a Matryoshka-trained model's vector cut to
its first `dim` coordinates and renormalised, a valid embedding of the same
text in a smaller space. The embedder serving such a label and the code
plane's twin tables both read the label and cut vectors the same way."""

from __future__ import annotations

import math
import re

_PREFIX_LABEL = re.compile(r"^prefix:(\d+):(.+)$")


def parse_prefix_label(label: str) -> tuple[int, str] | None:
    """`prefix:256:text-embedding-3-large` → (256, "text-embedding-3-large");
    None for any other label."""
    m = _PREFIX_LABEL.match((label or "").strip())
    if not m:
        return None
    dim = int(m.group(1))
    if dim <= 0:
        return None
    return dim, m.group(2).strip()


def project(vec: list[float], dim: int) -> list[float]:
    """The first `dim` coordinates, renormalised to unit length. An empty
    vector (a placeholder) stays empty; a shorter vector is an error — the
    twin would then hold a different space than its label claims."""
    if not vec:
        return []
    if len(vec) < dim:
        raise ValueError(f"vector of {len(vec)} dims cannot be cut to a {dim}-prefix")
    head = [float(x) for x in vec[:dim]]
    norm = math.sqrt(sum(x * x for x in head))
    if norm <= 0.0:
        return head
    return [x / norm for x in head]
