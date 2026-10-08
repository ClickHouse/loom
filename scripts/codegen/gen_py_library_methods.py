"""Regenerate `server/code/domain/extract/py_library_methods.py`.

The set is frozen in the tree so an interpreter upgrade cannot move edges;
run this, review the diff, and let the extractor stamp carry the change.
The set depends on the running interpreter, so CI does not run `--check`.

    python scripts/codegen/gen_py_library_methods.py
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # run as a file
    sys.path.insert(0, str(ROOT))

from scripts.codegen.common import GeneratedFile, generator_main  # noqa: E402

TYPES = [
    "builtins:str",
    "builtins:bytes",
    "builtins:bytearray",
    "builtins:memoryview",
    "builtins:dict",
    "builtins:list",
    "builtins:set",
    "builtins:frozenset",
    "builtins:tuple",
    "builtins:int",
    "builtins:float",
    "builtins:complex",
    "builtins:bool",
    "builtins:object",
    "builtins:type",
    "builtins:BaseException",
    "builtins:Exception",
    "builtins:range",
    "builtins:slice",
    "builtins:property",
    "builtins:staticmethod",
    "builtins:classmethod",
    "pathlib:Path",
    "pathlib:PurePath",
    "re:Pattern",
    "re:Match",
    "collections:deque",
    "collections:Counter",
    "collections:OrderedDict",
    "collections:defaultdict",
    "collections:ChainMap",
    "asyncio:Future",
    "asyncio:Task",
    "asyncio:Queue",
    "asyncio:Event",
    "asyncio:Lock",
    "asyncio:Semaphore",
    "asyncio:Condition",
    "asyncio:AbstractEventLoop",
    "asyncio:StreamReader",
    "asyncio:StreamWriter",
    "asyncio:Server",
    "asyncio.subprocess:Process",
    "threading:Thread",
    "threading:Lock",
    "threading:RLock",
    "threading:Event",
    "threading:Condition",
    "threading:Semaphore",
    "threading:Timer",
    "threading:local",
    "queue:Queue",
    "subprocess:Popen",
    "subprocess:CompletedProcess",
    "io:StringIO",
    "io:BytesIO",
    "io:TextIOWrapper",
    "io:BufferedReader",
    "io:BufferedWriter",
    "io:RawIOBase",
    "io:IOBase",
    "logging:Logger",
    "logging:Handler",
    "logging:LogRecord",
    "logging:Formatter",
    "logging:Filter",
    "argparse:ArgumentParser",
    "argparse:Namespace",
    "argparse:_SubParsersAction",
    "datetime:datetime",
    "datetime:date",
    "datetime:time",
    "datetime:timedelta",
    "datetime:timezone",
    "decimal:Decimal",
    "uuid:UUID",
    "socket:socket",
    "enum:Enum",
    "enum:IntEnum",
    "enum:Flag",
    "http.client:HTTPConnection",
    "http.client:HTTPResponse",
    "email.message:Message",
    "xml.etree.ElementTree:Element",
    "xml.etree.ElementTree:ElementTree",
    "concurrent.futures:Executor",
    "concurrent.futures:Future",
    "multiprocessing:Process",
    "multiprocessing:Pool",
    "selectors:BaseSelector",
    "ssl:SSLContext",
    "ssl:SSLSocket",
    "ipaddress:IPv4Address",
    "ipaddress:IPv4Network",
    "tempfile:TemporaryDirectory",
    "tempfile:NamedTemporaryFile",
    "zipfile:ZipFile",
    "tarfile:TarFile",
    "gzip:GzipFile",
    "csv:DictReader",
    "csv:DictWriter",
    "sqlite3:Connection",
    "sqlite3:Cursor",
    "unittest:TestCase",
    "unittest.mock:Mock",
    "unittest.mock:MagicMock",
    "unittest.mock:AsyncMock",
    "unittest.mock:patch",
    "weakref:WeakValueDictionary",
    "weakref:WeakSet",
    "hashlib:sha256",
    "hmac:HMAC",
    "random:Random",
    "array:array",
    "string:Template",
    "difflib:SequenceMatcher",
    "shlex:shlex",
    "tokenize:TokenInfo",
    "warnings:catch_warnings",
    "contextlib:ExitStack",
    "contextlib:AsyncExitStack",
    "traceback:TracebackException",
    "inspect:Signature",
    "inspect:Parameter",
    "typing:TextIO",
    "typing:IO",
    "typing:Generator",
    "typing:AsyncGenerator",
    "typing:MutableMapping",
    "typing:Mapping",
    "typing:MutableSequence",
    "typing:Sequence",
    "typing:MutableSet",
]
MODULES = [
    "json",
    "time",
    "os",
    "os.path",
    "sys",
    "random",
    "hashlib",
    "base64",
    "struct",
    "math",
    "statistics",
    "itertools",
    "functools",
    "contextlib",
    "tempfile",
    "shutil",
    "glob",
    "fnmatch",
    "textwrap",
    "csv",
    "re",
    "logging",
    "asyncio",
    "threading",
    "subprocess",
    "socket",
    "uuid",
    "inspect",
    "traceback",
    "warnings",
    "copy",
    "pprint",
    "shlex",
    "signal",
    "platform",
    "getpass",
    "secrets",
    "operator",
    "heapq",
    "bisect",
    "urllib.parse",
    "urllib.request",
    "datetime",
    "decimal",
    "io",
    "pathlib",
    "collections",
    "dataclasses",
    "typing",
    "abc",
    "enum",
    "unittest.mock",
    "concurrent.futures",
    "multiprocessing",
    "gzip",
    "zipfile",
    "tarfile",
    "sqlite3",
    "http.client",
    "ipaddress",
    "ssl",
    "selectors",
    "weakref",
    "string",
    "difflib",
    "tokenize",
    "numbers",
    "array",
]
_METHOD_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def collect() -> set[str]:
    names: set[str] = set()
    for spec in TYPES:
        mod, _, attr = spec.partition(":")
        try:
            t = getattr(importlib.import_module(mod), attr)
        except (ImportError, AttributeError):  # a type this interpreter lacks is skipped
            continue
        names |= {n for n in dir(t) if not n.startswith("__")}
    for mod in MODULES:
        try:
            m = importlib.import_module(mod)
        except ImportError:  # a module this interpreter lacks is skipped
            continue
        names |= {n for n in dir(m) if not n.startswith("_") and callable(getattr(m, n, None))}
    return {n for n in names if _METHOD_RE.match(n)}


_TABLE_HEAD = "PY_LIBRARY_METHODS: frozenset[str] = frozenset({"
TARGET = GeneratedFile(
    ROOT / "server" / "code" / "domain" / "extract" / "py_library_methods.py",
    "python scripts/codegen/gen_py_library_methods.py",
)


def build() -> str:
    """The module with its hand-written head kept and the name table replaced."""
    head, _sep, _tail = TARGET.path.read_text(encoding="utf-8").partition(_TABLE_HEAD)
    names = sorted(collect())
    head = re.sub(
        r"and review the diff; \d+ names\.", f"and review the diff; {len(names)} names.", head
    )
    body = "\n".join(f'    "{n}",' for n in names)
    return f"{head}{_TABLE_HEAD}\n{body}\n}})\n"


def main(argv: list[str] | None = None) -> int:
    return generator_main(__doc__, TARGET, build, argv)


if __name__ == "__main__":
    raise SystemExit(main())
