import tinylib

from app._vendor import tinylib as vendored
from app._vendor.tinylib import get_url


def run():
    return get_url("x"), vendored.get_url("y"), tinylib.get_url("z")
