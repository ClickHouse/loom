"""Use a pinned Python toolchain image for benchmark builds without apt downloads."""

from __future__ import annotations

import re

PYTHON_IMAGE = (
    "python:3.12-bookworm@sha256:4209fae09a3e20afd863e528bfddc235da5fd5977983e1996cd54cc9a72e6267"
)
COMPILER_INSTALL = "\\\n".join(
    (
        "RUN apt-get update && apt-get install -y --no-install-recommends ",
        "    build-essential ",
        "    && rm -rf /var/lib/apt/lists/*",
    )
)


def native_recipe(production: str) -> str:
    base = re.search(
        r"^FROM python:3\.12-slim@sha256:[0-9a-f]{64} AS py-builder$", production, re.MULTILINE
    )
    if base is None or production.count(COMPILER_INSTALL) != 1:
        raise ValueError("The locked production Python dependency recipe differs")
    return production.replace(base[0], "FROM " + PYTHON_IMAGE + " AS py-builder", 1).replace(
        COMPILER_INSTALL,
        "RUN gcc --version && git --version && "
        "python -c \"import ctypes; ctypes.CDLL('libgomp.so.1')\"",
        1,
    )
