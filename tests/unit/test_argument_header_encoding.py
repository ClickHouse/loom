"""Ignored names retain their identity without breaking the HTTP header codec."""

from types import SimpleNamespace
from unittest import TestCase
from urllib.parse import unquote

from pydantic import BaseModel
from starlette.datastructures import MutableHeaders

from server.transport.arg_check import header_value, unknown_args


class _Input(BaseModel):
    known: str = ""


class ArgumentHeaderEncoding(TestCase):
    def test_names_are_independent_ascii_header_components(self) -> None:
        op = SimpleNamespace(input_model=_Input)
        for name, expected in [
            ("plain_name", "plain_name"),
            ("caf\u00e9", "caf%C3%A9"),
            ("\u2603", "%E2%98%83"),
            ("a,b", "a%2Cb"),
            ("a%b", "a%25b"),
            ("line\r\nbreak", "line%0D%0Abreak"),
        ]:
            with self.subTest(name=name):
                args = {name: 1}
                value = header_value(op, args, {"ignored_args": ["known"]})
                self.assertEqual(value, "known," + expected)
                headers = MutableHeaders()
                headers["X-Loom-Ignored-Args"] = value
                self.assertTrue(value.isascii())
                self.assertEqual([unquote(part) for part in value.split(",")], ["known", name])
                self.assertEqual(unknown_args(_Input, args), [name])
