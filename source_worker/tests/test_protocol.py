import time
import unittest
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from loom_source_worker.protocol import (
    ConnectionBinding,
    ReadBinding,
    ReadGrant,
    ReadGrantRefused,
    issue_read_grant,
    verify_read_grant,
)


class TestReadGrant(unittest.TestCase):
    def setUp(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        self.binding = ReadBinding(
            worker_id=uuid4(),
            org="org",
            namespace="collection",
            source_id=uuid4(),
            selection_generation=5,
            requester="alice",
            connection_id=uuid4(),
            connection_generation=3,
            profile_digest="a" * 64,
            resource_id="urn:document:Résumé",
        )
        self.grant = ReadGrant(request_id=uuid4(), operation="live", binding=self.binding)

    def _verify(self, token: str) -> tuple[ReadGrant, int]:
        return verify_read_grant(
            token, issuer="loom", worker_id=self.binding.worker_id, key=self.key.public_key()
        )

    def _issue(self) -> str:
        return issue_read_grant(self.grant, issuer="loom", key=self.key, lifetime_seconds=20)

    def _sign(self, **claims: object) -> str:
        issued_at = int(time.time())
        return jwt.encode(
            {
                "iss": "loom",
                "aud": f"urn:loom:source-worker:{self.binding.worker_id}",
                "iat": issued_at,
                "exp": issued_at + 20,
                "grant": self.grant.model_dump(mode="json"),
                **claims,
            },
            self.key,
            algorithm="EdDSA",
        )

    def test_preserves_exact_selection_and_binding(self) -> None:
        actual, _ = self._verify(self._issue())
        self.assertEqual(actual, self.grant)

    def test_rejects_unsigned_or_another_worker_or_signer(self) -> None:
        alien_key = Ed25519PrivateKey.generate()
        for token in (
            jwt.encode({"grant": self.grant.model_dump(mode="json")}, "", algorithm="none"),
            self._sign(aud="urn:loom:source-worker:other"),
            issue_read_grant(self.grant, issuer="loom", key=alien_key, lifetime_seconds=20),
            self._sign(iss="another-loom"),
            self._sign(aud=[f"urn:loom:source-worker:{self.binding.worker_id}"]),
        ):
            with self.assertRaises(ReadGrantRefused):
                self._verify(token)

    def test_refuses_expired_future_and_long_lived_claims(self) -> None:
        now = int(time.time())
        cases = ({"exp": now - 1}, {"iat": now + 3}, {"exp": now + 90}, {"exp": str(now + 5)})
        for claims in cases:
            with self.assertRaises(ReadGrantRefused):
                self._verify(self._sign(**claims))

    def test_refuses_credential_or_coerced_generation_in_signed_payload(self) -> None:
        unsigned = self.grant.model_dump(mode="json")
        cases = (
            {"credential": "arbitrary"},
            {"selection_generation": "5"},
            {"connection_generation": True},
        )
        for overrides in cases:
            with self.assertRaises(ReadGrantRefused):
                binding = {**unsigned["binding"], **overrides}
                self._verify(self._sign(grant={**unsigned, "binding": binding}))

    def test_credential_can_only_resolve_for_the_exact_requester_and_generation(self) -> None:
        connection = ConnectionBinding(
            worker_id=self.binding.worker_id,
            org=self.binding.org,
            owner=self.binding.requester,
            connection_id=self.binding.connection_id,
            generation=self.binding.connection_generation,
            profile_digest=self.binding.profile_digest,
        )
        connection.authorize(self.grant)
        variants = (
            {"worker_id": uuid4()},
            {"org": "other"},
            {"owner": "bob"},
            {"connection_id": uuid4()},
            {"generation": 4},
            {"profile_digest": "b" * 64},
        )
        for values in variants:
            with self.subTest(fields=tuple(values)), self.assertRaises(ReadGrantRefused):
                connection.model_copy(update=values).authorize(self.grant)


if __name__ == "__main__":
    unittest.main()
