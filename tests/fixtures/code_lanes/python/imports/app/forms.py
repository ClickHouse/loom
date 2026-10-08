"""The import forms a module writes around its dependencies: a star import,
an optional dependency behind try / except ImportError, and a name imported
only for type checking."""
from typing import TYPE_CHECKING

from pkg.util.text import *  # noqa: F403

try:
    from pkg.impl import helper as fast_helper
except ImportError:  # the pure fallback
    from .local import sibling as fast_helper

if TYPE_CHECKING:
    from pkg.impl import Store


def render(raw):
    decode(raw)  # bound through the star import  # noqa: F405
    fast_helper()  # either branch's definition
    return raw


def persist(store: "Store"):
    return store.save()  # the annotation's class, imported for type checking only
