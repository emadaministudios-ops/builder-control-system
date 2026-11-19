"""Server-side bearer-token authentication for the BCS control API.

Only SHA-256 token digests are configured in the process environment. Principal
identity, actor type and scopes are derived from that trusted configuration;
request bodies and caller-supplied identity headers are never authoritative.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import os
import re
from typing import Mapping

from .approvals import ActorType


@dataclass(frozen=True)
class Principal:
    actor_id: str
    actor_type: ActorType
    scopes: frozenset[str]


class AuthenticationError(ValueError):
    """Raised when a bearer token is missing, malformed or unknown."""


class TokenAuthenticator:
    def __init__(self, token_digests: Mapping[str, Principal]):
        normalized: dict[str, Principal] = {}
        for digest, principal in token_digests.items():
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("Configured token digests must be lowercase SHA-256 hex.")
            if not isinstance(principal, Principal):
                raise TypeError("Each token digest must map to a Principal.")
            if not principal.actor_id.strip() or not principal.scopes:
                raise ValueError("Principal identity and at least one scope are required.")
            if any(not isinstance(scope, str) or not scope.strip() for scope in principal.scopes):
                raise ValueError("Principal scopes must be non-empty strings.")
            if digest in normalized:
                raise ValueError("Duplicate token digest configuration.")
            normalized[digest] = principal
        self._entries = tuple(normalized.items())

    @classmethod
    def from_environment(cls, variable: str = "BCS_API_TOKEN_HASHES") -> "TokenAuthenticator":
        raw = os.environ.get(variable, "")
        if not raw.strip():
            return cls({})
        try:
            entries = json.loads(raw)
            if not isinstance(entries, list):
                raise ValueError("Token configuration must be a JSON array.")
            mapped = {}
            for item in entries:
                if not isinstance(item, dict):
                    raise ValueError("Each token configuration entry must be an object.")
                digest = item["token_sha256"]
                principal = Principal(
                    actor_id=item["actor_id"],
                    actor_type=ActorType(item["actor_type"]),
                    scopes=frozenset(item["scopes"]),
                )
                if digest in mapped:
                    raise ValueError("Duplicate token digest configuration.")
                mapped[digest] = principal
            return cls(mapped)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("BCS API token configuration is invalid.") from exc

    @property
    def configured(self) -> bool:
        return bool(self._entries)

    def authenticate(self, authorization: str | None) -> Principal:
        if not self.configured:
            raise RuntimeError("BCS API authentication is not configured.")
        if not isinstance(authorization, str):
            raise AuthenticationError("Bearer authentication is required.")
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token or token.strip() != token:
            raise AuthenticationError("Bearer authentication is required.")
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        matched = None
        for expected, principal in self._entries:
            if hmac.compare_digest(digest, expected):
                matched = principal
        if matched is None:
            raise AuthenticationError("Invalid bearer token.")
        return matched

    @staticmethod
    def require_scope(principal: Principal, scope: str) -> None:
        if scope not in principal.scopes:
            raise PermissionError("The authenticated principal lacks the required scope.")


__all__ = [
    "Principal", "AuthenticationError", "TokenAuthenticator",
]
