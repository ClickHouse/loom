
from srcpkg._speed import fast
from srcpkg.core import run


def main() -> int:
    print(run(), fast(3))
    return 0
