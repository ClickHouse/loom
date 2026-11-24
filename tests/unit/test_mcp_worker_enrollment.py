import time
import unittest
from dataclasses import replace
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from server.knowledge.app.mcp_worker_enrollment import (
    EnrollmentChallenge,
    EnrollmentRefused,
    check_enrollment_write,
    issue_enrollment_grant,
    verify_enrollment,
)


class TestWorkerEnrollment(unittest.TestCase):
    def setUp(self) -> None:
        now = int(time.time())
        self.key = Ed25519PrivateKey.generate()
        self.connection = uuid4()
        self.challenge = EnrollmentChallenge(
            "org",
            "alice",
            uuid4(),
            "a" * 64,
            uuid4(),
            now - 2,
            now + 30,
            "urn:loom:enroll:test",
            self.connection,
            2,
        )
        self.claims = {
            "iss": f"urn:loom:source-worker:{self.challenge.worker_id}",
            "aud": self.challenge.audience,
            "iat": now - 1,
            "exp": now + 15,
            "binding": {
                "org": self.challenge.org,
                "requester": self.challenge.requester,
                "worker_id": str(self.challenge.worker_id),
                "profile_digest": self.challenge.profile_digest,
                "nonce": str(self.challenge.nonce),
                "connection_id": str(self.connection),
                "generation": 2,
                "resource_space": "b" * 64,
            },
        }

    def _verify(self, claims, *, key=None):
        return verify_enrollment(
            self.challenge,
            jwt.encode(claims, key or self.key, algorithm="EdDSA"),
            verifier=self.key.public_key(),
        )

    def test_statement_yields_only_the_owned_opaque_connection(self) -> None:
        approved = self._verify(self.claims)
        self.assertEqual(approved.binding.connection_id, self.connection)
        self.assertEqual(approved.binding.owner, "alice")
        self.assertEqual(approved.binding.connection_generation, 2)
        self.assertEqual(approved.resource_space, "b" * 64)
        self.assertTrue(approved.active)

    def test_other_readers_workers_and_unapproved_metadata_are_refused(self) -> None:
        for field, wrong in (
            ("requester", "bob"),
            ("org", "other-org"),
            ("worker_id", str(uuid4())),
            ("profile_digest", "c" * 64),
            ("nonce", str(uuid4())),
            ("connection_id", str(uuid4())),
            ("generation", 3),
            ("generation", True),
            ("token", "synthetic-unexpected-field"),
        ):
            binding = {**self.claims["binding"], field: wrong}
            with self.subTest(field=field), self.assertRaises(EnrollmentRefused):
                self._verify({**self.claims, "binding": binding})

    def test_worker_key_audience_and_enrollment_window_are_pinned(self) -> None:
        for claims in (
            {**self.claims, "aud": "urn:loom:enroll:other"},
            {**self.claims, "exp": self.challenge.expires_at + 1},
            {**self.claims, "iat": self.challenge.issued_at - 1},
        ):
            with self.assertRaises(EnrollmentRefused):
                self._verify(claims)
        with self.assertRaises(EnrollmentRefused):
            self._verify(self.claims, key=Ed25519PrivateKey.generate())

    def test_link_retry_cannot_restore_a_revoked_or_different_account(self) -> None:
        link = self._verify(self.claims)
        self.assertTrue(check_enrollment_write(None, link))
        self.assertFalse(check_enrollment_write(link, link))
        revoked = replace(link, active=False)
        with self.assertRaises(EnrollmentRefused):
            check_enrollment_write(revoked, link)
        rotated = replace(link, binding=replace(link.binding, connection_generation=3))
        self.assertTrue(check_enrollment_write(revoked, rotated))
        with self.assertRaises(EnrollmentRefused):
            check_enrollment_write(rotated, link)
        with self.assertRaises(EnrollmentRefused):
            check_enrollment_write(link, replace(rotated, resource_space="c" * 64))

    def test_grant_carries_exact_account_and_no_credential(self) -> None:
        token = issue_enrollment_grant(self.challenge, issuer="loom", key=self.key)
        claims = jwt.decode(
            token,
            self.key.public_key(),
            algorithms=["EdDSA"],
            issuer="loom",
            audience=self.challenge.audience,
        )
        self.assertEqual(claims["operation"], "enroll")
        self.assertEqual(
            claims["binding"],
            {
                key: value
                for key, value in self.claims["binding"].items()
                if key != "resource_space"
            },
        )
        self.assertLessEqual(claims["exp"] - claims["iat"], 30)
