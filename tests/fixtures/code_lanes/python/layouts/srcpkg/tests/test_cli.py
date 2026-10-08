from fakes import FakeStream
from helpers import make
from srcpkg.cli import main


def test_main(cli_args):
    assert make() == {}
    assert FakeStream().write("x") == 1
    assert main() == 0
