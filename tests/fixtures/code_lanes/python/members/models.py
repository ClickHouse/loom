"""Members fixture: dataclass fields, an Enum, module bindings."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TypeAlias

__all__ = ["Color", "Point", "DEFAULT_COLOR"]

DEFAULT_COLOR = "red"
MAX_POINTS: int = 100
Coords: TypeAlias = tuple[float, float]  # noqa: UP040 — the annotation form is the case under test
Registry = dict[str, "Point"]


class Color(Enum):
    RED = "red"
    GREEN = "green"


@dataclass
class Point:
    x: float
    y: float = 0.0
    tags: list[str] = field(default_factory=list)

    def moved(self, dx: float) -> Point:
        return Point(self.x + dx, self.y)


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self.first, self.last = None, None
        self.count: int = 0

    def add(self, p: Point) -> None:
        self.count += 1

    def query_all(self, sql: str) -> list[Point]:
        return []
