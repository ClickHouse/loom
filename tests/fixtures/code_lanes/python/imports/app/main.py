"""Every import form the rung binds, and the
library calls it must leave alone."""
import json

import pkg.util
import pkg.util.text as txt
from pkg import helper
from pkg.impl import Store

from .local import sibling


def run(raw, store):
    json.dumps(raw)  # the standard library's: no edge, whatever `dumps` the corpus defines
    txt.dumps(raw)  # the aliased module's dumps, 1.0
    pkg.util.text.decode(raw)  # the dotted module's decode, 1.0
    helper()  # re-exported by pkg/__init__.py, 0.9
    Store.make()  # the imported class's classmethod, 1.0
    sibling()  # a relative import, 1.0
    raw.decode()  # an untyped receiver's library method: no edge
    s = Store()
    s.save()  # a typed receiver, 0.9
    return raw.encode().decode()  # an expression's method: no edge


def lazy():
    from pkg.util.text import decode

    return decode(b"")  # the function-body import binds too, 1.0
