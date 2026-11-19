"""Tests for server-side BCS API authentication."""
import hashlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.approvals import ActorType
from bcs.auth import AuthenticationError, Principal, TokenAuthenticator


class TokenAuthenticatorTests(unittest.TestCase):
    def setUp(self):
        self.token = "a-long-random-test-token"
        digest = hashlib.sha256(self.token.encode()).hexdigest()
        self.principal = Principal("owner-1", ActorType.HUMAN, frozenset({"tasks:read", "approvals:decide"}))
        self.auth = TokenAuthenticator({digest: self.principal})

    def test_valid_bearer_resolves_server_configured_principal(self):
        principal = self.auth.authenticate(f"Bearer {self.token}")
        self.assertEqual(principal, self.principal)

    def test_missing_malformed_and_unknown_tokens_are_rejected(self):
        for header in (None, "", "Basic abc", "Bearer", "Bearer wrong", f"Bearer {self.token} "):
            with self.subTest(header=header):
                with self.assertRaises(AuthenticationError):
                    self.auth.authenticate(header)

    def test_scope_is_checked_against_authenticated_principal(self):
        TokenAuthenticator.require_scope(self.principal, "tasks:read")
        with self.assertRaises(PermissionError):
            TokenAuthenticator.require_scope(self.principal, "tasks:write")

    def test_unconfigured_authenticator_fails_closed(self):
        with self.assertRaises(RuntimeError):
            TokenAuthenticator({}).authenticate(f"Bearer {self.token}")

    def test_environment_uses_hashes_and_server_owned_identity(self):
        digest = hashlib.sha256(self.token.encode()).hexdigest()
        config = [{
            "token_sha256": digest,
            "actor_id": "configured-owner",
            "actor_type": "human",
            "scopes": ["approvals:decide"],
        }]
        with patch.dict(os.environ, {"BCS_API_TOKEN_HASHES": json.dumps(config)}):
            loaded = TokenAuthenticator.from_environment()
        principal = loaded.authenticate(f"Bearer {self.token}")
        self.assertEqual(principal.actor_id, "configured-owner")
        self.assertEqual(principal.actor_type, ActorType.HUMAN)

    def test_invalid_environment_configuration_is_rejected(self):
        with patch.dict(os.environ, {"BCS_API_TOKEN_HASHES": "not-json"}):
            with self.assertRaises(ValueError):
                TokenAuthenticator.from_environment()


if __name__ == "__main__":
    unittest.main()
