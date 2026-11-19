"""Tests for bounded advisory specialist consultation."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.consultation import ConsultationError, SpecialistConsultant
from bcs.providers import ProviderResponse


class FakeProviders:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return ProviderResponse("fake", "fake-model", self.content)


class SpecialistConsultationTests(unittest.TestCase):
    def test_returns_structured_advice_without_tools_or_authority(self):
        providers = FakeProviders(
            '{"advice":"Use an adapter boundary.","risks":["Coupling"],'
            '"next_steps":["Define the interface."],"confidence":"high"}'
        )
        result = SpecialistConsultant(providers=providers).consult(
            requesting_role="frontend-developer", specialist_role="architect",
            objective="Integrate authentication.", question="Where should token refresh live?",
            context="The UI currently calls the API directly.",
        )
        self.assertEqual(result.advice, "Use an adapter boundary.")
        self.assertEqual(result.risks, ("Coupling",))
        self.assertEqual(len(providers.calls), 1)
        self.assertIn("Do not claim to inspect files", providers.calls[0][0][0]["content"])

    def test_rejects_unapproved_role_before_provider_call(self):
        providers = FakeProviders("{}")
        with self.assertRaises(ConsultationError):
            SpecialistConsultant(providers=providers).consult(
                requesting_role="frontend-developer", specialist_role="root-admin",
                objective="Do something", question="Can you help?",
            )
        self.assertEqual(providers.calls, [])

    def test_enforces_per_consultant_call_limit(self):
        providers = FakeProviders(
            '{"advice":"Use an adapter.","risks":[],"next_steps":[],"confidence":"medium"}'
        )
        consultant = SpecialistConsultant(providers=providers, max_calls=1)
        args = dict(
            requesting_role="frontend-developer", specialist_role="architect",
            objective="Design", question="Review?",
        )
        consultant.consult(**args)
        with self.assertRaises(ConsultationError):
            consultant.consult(**args)
        self.assertEqual(len(providers.calls), 1)

    def test_rejects_self_consultation(self):
        with self.assertRaises(ConsultationError):
            SpecialistConsultant(providers=FakeProviders("{}")).consult(
                requesting_role="architect", specialist_role="architect",
                objective="Design", question="Review this?",
            )

    def test_rejects_invalid_model_output(self):
        with self.assertRaises(ConsultationError):
            SpecialistConsultant(providers=FakeProviders(
                '{"advice":"Looks good","risks":"none","next_steps":[],"confidence":"high"}'
            )).consult(
                requesting_role="frontend-developer", specialist_role="architect",
                objective="Design", question="Review?",
            )


if __name__ == "__main__":
    unittest.main()
