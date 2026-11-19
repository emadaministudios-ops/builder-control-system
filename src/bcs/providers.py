"""Bounded OpenAI-compatible chat provider adapter.

Supports cloud endpoints and local Ollama-compatible endpoints without adding
runtime dependencies. Credentials are read from the environment at call time and
are never included in errors or returned data.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class ProviderError(RuntimeError):
    """Raised when a model provider request cannot be completed safely."""


@dataclass(frozen=True)
class ProviderConfig:
    provider_id: str
    base_url: str
    model: str
    api_key_env: str | None = None
    timeout_seconds: float = 30.0
    max_response_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        if not self.provider_id.strip() or not self.model.strip():
            raise ValueError("Provider ID and model are required.")
        if not isinstance(self.base_url, str) or not self.base_url.strip():
            raise ValueError("Provider base URL is required.")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Provider URL must be an absolute HTTP(S) URL.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Provider URL cannot contain credentials, query, or fragment.")
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and parsed.hostname not in local_hosts:
            raise ValueError("Non-local model providers must use HTTPS.")
        if not isinstance(self.timeout_seconds, (int, float)) or self.timeout_seconds <= 0:
            raise ValueError("Provider timeout must be positive.")
        if (
            isinstance(self.max_response_bytes, bool)
            or not isinstance(self.max_response_bytes, int)
            or self.max_response_bytes < 1024
        ):
            raise ValueError("Response byte limit must be an integer of at least 1024.")
        if self.api_key_env is not None and not self.api_key_env.strip():
            raise ValueError("API key environment variable name cannot be blank.")


@dataclass(frozen=True)
class ProviderResponse:
    provider_id: str
    model: str
    content: str
    usage: dict | None = None


class OpenAICompatibleProvider:
    """Minimal chat-completions adapter for OpenAI-compatible cloud/local APIs."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    @staticmethod
    def _validate_messages(messages) -> list[dict[str, str]]:
        if not isinstance(messages, (list, tuple)) or not messages:
            raise ValueError("At least one chat message is required.")
        normalized = []
        for message in messages:
            if not isinstance(message, dict):
                raise ValueError("Each chat message must be an object.")
            role = message.get("role")
            content = message.get("content")
            if role not in {"system", "user", "assistant"}:
                raise ValueError("Unsupported chat message role.")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Chat message content must be non-empty text.")
            if len(content) > 100_000:
                raise ValueError("Chat message exceeds the configured size limit.")
            normalized.append({"role": role, "content": content})
        return normalized

    def complete(
        self,
        messages,
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> ProviderResponse:
        normalized = self._validate_messages(messages)
        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not 0 <= temperature <= 2
        ):
            raise ValueError("Temperature must be between 0 and 2.")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= 32768:
            raise ValueError("max_tokens must be between 1 and 32768.")

        payload = json.dumps({
            "model": self.config.model,
            "messages": normalized,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }, separators=(",", ":"), allow_nan=False).encode("utf-8")
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.config.api_key_env:
            api_key = os.environ.get(self.config.api_key_env)
            if not api_key:
                raise ProviderError(
                    f"Required credential environment variable "
                    f"{self.config.api_key_env} is not configured."
                )
            headers["Authorization"] = f"Bearer {api_key}"

        request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(
                request, timeout=self.config.timeout_seconds
            ) as response:
                raw = response.read(self.config.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise ProviderError("Model provider returned an HTTP error.") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError("Could not reach the configured model provider.") from None

        if len(raw) > self.config.max_response_bytes:
            raise ProviderError("Model provider response exceeded the configured size limit.")
        try:
            data = json.loads(raw.decode("utf-8"))
            choices = data["choices"]
            content = choices[0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Empty model response.")
            usage = data.get("usage")
            if usage is not None and not isinstance(usage, dict):
                usage = None
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
            raise ProviderError("Model provider returned an invalid chat response.") from None

        return ProviderResponse(
            provider_id=self.config.provider_id,
            model=str(data.get("model") or self.config.model),
            content=content,
            usage=usage,
        )


__all__ = [
    "ProviderError", "ProviderConfig", "ProviderResponse",
    "OpenAICompatibleProvider",
]
