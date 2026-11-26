"""Independent endpoint-identity cases used by the narrow mutation pilot."""

from __future__ import annotations

from unittest import TestCase
from urllib.parse import parse_qsl

from server.core import embedding_space
from tests.fuzz.embedding_identity_oracle import check_input, configuration


class PublicEndpointIdentity(TestCase):
    def test_generated_byte_cases_obey_the_public_contract(self) -> None:
        for value in range(256):
            check_input(bytes([value]))

    def test_empty_endpoint_keeps_the_other_identity_fields(self) -> None:
        self.assertEqual(
            embedding_space.public_identity(configuration("")),
            ("custom", "synthetic-model", 16, "", True),
        )

    def test_credentials_unknown_query_and_fragment_are_removed(self) -> None:
        url = "HTTPS://person:secret@EXAMPLE.INVALID:443/v1///?MODEL=x&api-version=1&password=private#secret"
        expected = "https://example.invalid:443/v1?MODEL=x&api-version=1"
        self.assertEqual(embedding_space.public_identity(configuration(url))[3], expected)

    def test_allowed_query_names_and_duplicate_values_remain_distinct(self) -> None:
        url = "https://example.invalid/?model=a&model=b&api_version=v&deployment=d&dimensions=16"
        public = embedding_space.public_identity(configuration(url))[3]
        self.assertEqual(
            parse_qsl(public.split("?", 1)[1]),
            [
                ("model", "a"),
                ("model", "b"),
                ("api_version", "v"),
                ("deployment", "d"),
                ("dimensions", "16"),
            ],
        )

    def test_ipv6_authority_retains_brackets_and_port(self) -> None:
        url = "https://user:secret@[2001:db8::1]:443/v1/#fragment"
        self.assertEqual(
            embedding_space.public_identity(configuration(url))[3], "https://[2001:db8::1]:443/v1"
        )

    def test_malformed_bracket_refuses_instead_of_publishing(self) -> None:
        with self.assertRaises(ValueError):
            embedding_space.public_identity(configuration("https://[invalid/"))

    def test_every_userinfo_separator_before_the_final_host_is_removed(self) -> None:
        url = "https://first:synthetic@alias@example.invalid/v1/?model=m"
        self.assertEqual(
            embedding_space.public_identity(configuration(url))[3],
            "https://example.invalid/v1?model=m",
        )

    def test_a_meaningful_uppercase_path_suffix_is_preserved(self) -> None:
        url = "https://example.invalid/pathX///?model=m"
        self.assertEqual(
            embedding_space.public_identity(configuration(url))[3],
            "https://example.invalid/pathX?model=m",
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
