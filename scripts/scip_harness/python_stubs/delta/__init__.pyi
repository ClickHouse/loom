# Stand-in for delta-spark (`import delta`), which no indexing environment
# installs. scip-python 0.6.6's pyright asserts in getTypeFromWildcardImport
# when `from delta import *` names an unresolved module and a name from it is
# checked against a resolved signature: the whole index then fails. With this
# stub on the search path the import resolves and every name is Any.
from typing import Any

def __getattr__(name: str) -> Any: ...
