"""Tests for BRAIN planning and deterministic dependency validation."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.planner import PlanningError, TaskPlanner


class FakeProviders:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        class Response:
            provider_id = "fake"
            model = "fake-model"
            def __init__(self, content):
                self.content = content
        return Response(self.content)


def step(step_id, role, objective, criterion, depends_on=()):
    return {
        "step_id": step_id,
        "role": role,
        "objective": objective,
        "acceptance_criteria": [criterion],
        "depends_on": list(depends_on),
    }


class TaskPlannerTests(unittest.TestCase):
    def planner(self, steps):
        providers = FakeProviders(json.dumps({"steps": steps}))
        return TaskPlanner(providers=providers), providers

    def test_validates_and_orders_dependency_graph(self):
        planner, _ = self.planner([
            step("implement", "frontend-developer", "Implement the modal.", "Modal closes after selection.", ("design",)),
            step("design", "architect", "Design modal state behavior.", "Modal state behavior is documented."),
            step("api", "backend-developer", "Implement independent API changes.", "API contract is implemented."),
        ])
        plan = planner.plan(
            "Fix the modal and API.",
            ["Modal closes after selection."],
        )
        self.assertEqual(plan.execution_order, ("api", "design", "implement"))
        self.assertEqual(plan.steps[0].step_id, "implement")

    def test_rejects_dependency_cycle(self):
        planner, _ = self.planner([
            step("first", "architect", "First step.", "First is ready.", ("second",)),
            step("second", "builder", "Second step.", "Second is ready.", ("first",)),
        ])
        with self.assertRaises(PlanningError):
            planner.plan("Build feature.")

    def test_rejects_unknown_dependency(self):
        planner, _ = self.planner([
            step("first", "builder", "Build feature.", "Feature works.", ("missing",)),
        ])
        with self.assertRaises(PlanningError):
            planner.plan("Build feature.")

    def test_rejects_unsupported_role(self):
        planner, _ = self.planner([
            step("first", "super-admin", "Build feature.", "Feature works."),
        ])
        with self.assertRaises(PlanningError):
            planner.plan("Build feature.")

    def test_rejects_plan_that_drops_original_acceptance_criterion(self):
        planner, _ = self.planner([
            step("first", "builder", "Build feature.", "Something else works."),
        ])
        with self.assertRaises(PlanningError):
            planner.plan("Build feature.", ["The login flow remains intact."])

    def test_rejects_duplicate_step_ids(self):
        planner, _ = self.planner([
            step("first", "builder", "Build A.", "A works."),
            step("first", "builder", "Build B.", "B works."),
        ])
        with self.assertRaises(PlanningError):
            planner.plan("Build feature.")

    def test_requires_a_valid_json_object(self):
        providers = FakeProviders("not json")
        planner = TaskPlanner(providers=providers)
        with self.assertRaises(PlanningError):
            planner.plan("Build feature.")


if __name__ == "__main__":
    unittest.main()
