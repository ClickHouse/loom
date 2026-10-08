"""Members fixture: property / classmethod / staticmethod, an override."""
from abc import abstractmethod
from typing import Protocol

from .models import Point, Store


class Shape:
    @property
    def area(self) -> float:
        return 0.0

    @classmethod
    def unit(cls) -> "Shape":
        return cls()

    @staticmethod
    def name() -> str:
        return "shape"

    @abstractmethod
    def draw(self) -> None: ...


class Square(Shape):
    def __init__(self, side: float) -> None:
        self.side = side

    @property
    def area(self) -> float:
        return self.side * self.side

    def draw(self) -> None:
        Point(0, 0).moved(self.side)


class FakeStore(Store):
    def query_all(self, sql: str) -> list[Point]:
        self.count += 1
        return [Point(1, 2)]


class Drawable(Protocol):
    def draw(self) -> None:
        """A protocol method: a declaration, not a unit."""
        ...


async def render_shape(shape: Shape) -> float:
    shape.draw()
    return shape.area
