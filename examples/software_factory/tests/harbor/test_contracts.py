"""Adapter requests must use fields accepted by the serving operation models."""

import unittest

from examples.software_factory.harbor.contracts import requests
from examples.software_factory.harbor.native_setup import validate_requests


class RequestContracts(unittest.TestCase):
    def test_code_and_memory_shapes_are_accepted(self):
        self.assertEqual(validate_requests(requests())["validated"], 15)

    def test_unsupported_lesson_fields_refuse_before_a_dispatch(self):
        for operation in ("code.enclosing",):
            with self.subTest(operation=operation):
                row = next(row for row in requests() if row["operation"] == operation)
                row["body"]["include_lessons"] = False
                with self.assertRaisesRegex(ValueError, "undeclared arguments"):
                    validate_requests([row])
