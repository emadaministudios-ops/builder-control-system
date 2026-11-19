import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.providers import (
    OpenAICompatibleProvider, ProviderConfig, ProviderError,
    ProviderRegistry, provider_configs_from_environment,
)


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def read(self, size=-1):
        return self.body[:size]


def response_body(content="Hello from model"):
    return json.dumps({
        "model": "test-model",
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 3},
    }).encode()


class ProviderConfigTests(unittest.TestCase):
    def test_local_ollama_endpoint_allows_plain_http(self):
        config = ProviderConfig(
            provider_id="ollama-local",
            base_url="http://127.0.0.1:11434/v1",
            model="llama3.2",
        )
        self.assertEqual(config.model, "llama3.2")

    def test_remote_provider_requires_https(self):
        with self.assertRaises(ValueError):
            ProviderConfig(
                provider_id="remote", base_url="http://models.example.com/v1",
                model="model",
            )

    def test_url_credentials_and_query_are_rejected(self):
        for url in (
            "https://user:secret@models.example.com/v1",
            "https://models.example.com/v1?token=secret",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ProviderConfig("remote", url, "model")


class OpenAICompatibleProviderTests(unittest.TestCase):
    def test_ollama_style_chat_request(self):
        config = ProviderConfig(
            "ollama", "http://127.0.0.1:11434/v1/", "llama3.2"
        )
        provider = OpenAICompatibleProvider(config)
        fake = FakeResponse(response_body())
        with patch("urllib.request.urlopen", return_value=fake) as open_url:
            result = provider.complete([{"role": "user", "content": "Hi"}])
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:11434/v1/chat/completions")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(result.content, "Hello from model")
        self.assertEqual(result.provider_id, "ollama")

    def test_gpt6_cloud_provider_uses_reasoning_compatible_parameters(self):
        config = ProviderConfig(
            "cloud", "https://api.openai.com/v1", "gpt-6.2-sol",
            api_key_env="BCS_TEST_MODEL_KEY",
        )
        fake = FakeResponse(response_body())
        with patch.dict(os.environ, {"BCS_TEST_MODEL_KEY": "test-secret"}):
            with patch("urllib.request.urlopen", return_value=fake) as open_url:
                OpenAICompatibleProvider(config).complete(
                    [{"role": "user", "content": "Hi"}], max_tokens=256
                )
        request = open_url.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["max_completion_tokens"], 256)
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertNotIn("temperature", payload)
        self.assertNotIn("max_tokens", payload)

    def test_cloud_provider_uses_environment_credential(self):
        config = ProviderConfig(
            "cloud", "https://api.example.com/v1", "cloud-model",
            api_key_env="BCS_TEST_MODEL_KEY",
        )
        fake = FakeResponse(response_body())
        with patch.dict(os.environ, {"BCS_TEST_MODEL_KEY": "test-secret"}):
            with patch("urllib.request.urlopen", return_value=fake) as open_url:
                OpenAICompatibleProvider(config).complete(
                    [{"role": "user", "content": "Hi"}]
                )
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")

    def test_missing_cloud_credential_fails_without_network(self):
        config = ProviderConfig(
            "cloud", "https://api.example.com/v1", "cloud-model",
            api_key_env="BCS_MISSING_TEST_KEY",
        )
        with patch.dict(os.environ, {}, clear=True):
            with patch("urllib.request.urlopen") as open_url:
                with self.assertRaises(ProviderError):
                    OpenAICompatibleProvider(config).complete(
                        [{"role": "user", "content": "Hi"}]
                    )
                open_url.assert_not_called()

    def test_invalid_message_is_rejected_before_network(self):
        provider = OpenAICompatibleProvider(
            ProviderConfig("local", "http://localhost:11434/v1", "model")
        )
        with patch("urllib.request.urlopen") as open_url:
            with self.assertRaises(ValueError):
                provider.complete([{"role": "tool", "content": "unsafe"}])
            open_url.assert_not_called()

    def test_oversized_response_is_rejected(self):
        provider = OpenAICompatibleProvider(ProviderConfig(
            "local", "http://localhost:11434/v1", "model",
            max_response_bytes=1024,
        ))
        with patch("urllib.request.urlopen", return_value=FakeResponse(b"x" * 1025)):
            with self.assertRaises(ProviderError):
                provider.complete([{"role": "user", "content": "Hi"}])

    def test_malformed_response_is_rejected(self):
        provider = OpenAICompatibleProvider(
            ProviderConfig("local", "http://localhost:11434/v1", "model")
        )
        with patch("urllib.request.urlopen", return_value=FakeResponse(b"not-json")):
            with self.assertRaises(ProviderError):
                provider.complete([{"role": "user", "content": "Hi"}])

    def test_registry_requires_explicit_selection_when_empty(self):
        registry = ProviderRegistry()
        with self.assertRaises(ProviderError):
            registry.complete([{"role": "user", "content": "Hi"}])

    def test_registry_rejects_unknown_provider_without_fallback(self):
        registry = ProviderRegistry()
        registry.register(ProviderConfig(
            "ollama", "http://localhost:11434/v1", "llama3.2"
        ))
        with self.assertRaises(ProviderError):
            registry.complete(
                [{"role": "user", "content": "Hi"}], provider_id="missing"
            )

    def test_environment_loads_both_local_and_cloud_provider_definitions(self):
        values = {
            "BCS_OLLAMA_MODEL": "llama3.2",
            "BCS_CLOUD_MODEL": "cloud-model",
            "BCS_CLOUD_BASE_URL": "https://api.example.com/v1",
            "BCS_CLOUD_API_KEY_ENV": "MY_MODEL_KEY",
        }
        with patch.dict(os.environ, values, clear=True):
            configs = provider_configs_from_environment()
        self.assertEqual({item.provider_id for item in configs}, {"ollama", "cloud"})
        cloud = next(item for item in configs if item.provider_id == "cloud")
        self.assertEqual(cloud.api_key_env, "MY_MODEL_KEY")

    def test_partial_cloud_configuration_is_rejected(self):
        with patch.dict(os.environ, {"BCS_CLOUD_MODEL": "cloud-model"}, clear=True):
            with self.assertRaises(ValueError):
                provider_configs_from_environment()

    def test_http_error_does_not_leak_provider_body(self):
        import urllib.error
        config = ProviderConfig("cloud", "https://api.example.com/v1", "model")
        provider = OpenAICompatibleProvider(config)
        error = urllib.error.HTTPError(
            "https://api.example.com", 401, "Unauthorized", {}, None
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(ProviderError) as caught:
                provider.complete([{"role": "user", "content": "Hi"}])
        self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
