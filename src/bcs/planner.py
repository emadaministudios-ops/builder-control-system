"""BRAIN task planning: turn an objective into a validated dependency graph.

The model proposes a plan, but deterministic validation owns accepted roles,
IDs, dependency integrity, graph ordering, and task creation boundaries.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

from .providers import ProviderError, ProviderRegistry


_ALLOWED_ROLES = frozenset({
    "architect", "requirements-analyst", "frontend-developer",
    "backend-developer", "integration-engineer", "qa-engineer",
    "documentation-agent", "builder", "research-helper",
})


@dataclass(frozen=True)
class PlanStep:
    step_id: str
    role: str
    objective: str
    acceptance_criteria: tuple[str, ...]
    depends_on: tuple[str, ...]


@dataclass(frozen=True)
class TaskPlan:
    objective: str
    acceptance_criteria: tuple[str, ...]
    steps: tuple[PlanStep, ...]
    execution_order: tuple[str, ...]


class PlanningError(ValueError):
    """Raised when a model-generated plan is invalid or unsafe."""


class TaskPlanner:
    def __init__(
        self,
        *,
        providers: ProviderRegistry,
        provider_id: str | None = None,
        max_steps: int = 30,
        max_criteria_per_step: int = 20,
        max_tokens: int = 6000,
    ) -> None:
        if not 1 <= max_steps <= 100:
            raise ValueError("max_steps must be between 1 and 100.")
        if not 1 <= max_criteria_per_step <= 50:
            raise ValueError("max_criteria_per_step must be between 1 and 50.")
        if not 1 <= max_tokens <= 32768:
            raise ValueError("max_tokens must be between 1 and 32768.")
        self.providers = providers
        self.provider_id = provider_id
        self.max_steps = max_steps
        self.max_criteria_per_step = max_criteria_per_step
        self.max_tokens = max_tokens

    @staticmethod
    def _topological_order(steps: tuple[PlanStep, ...]) -> tuple[str, ...]:
        by_id = {step.step_id: step for step in steps}
        indegree = {step.step_id: len(step.depends_on) for step in steps}
        followers = {step.step_id: [] for step in steps}
        for step in steps:
            for dependency in step.depends_on:
                if dependency not in by_id:
                    raise PlanningError(f"Step {step.step_id} depends on an unknown step.")
                followers[dependency].append(step.step_id)
        ready = sorted(step_id for step_id, count in indegree.items() if count == 0)
        order = []
        while ready:
            current = ready.pop(0)
            order.append(current)
            for follower in sorted(followers[current]):
                indegree[follower] -= 1
                if indegree[follower] == 0:
                    ready.append(follower)
                    ready.sort()
        if len(order) != len(steps):
            raise PlanningError("The proposed task plan contains a dependency cycle.")
        return tuple(order)

    def _validate(self, raw: str, objective: str, criteria: tuple[str, ...]) -> TaskPlan:
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise PlanningError("Planner response was not valid JSON.") from exc
        if not isinstance(data, dict) or set(data) != {"steps"}:
            raise PlanningError("Planner response must contain exactly one steps array.")
        items = data["steps"]
        if not isinstance(items, list) or not 1 <= len(items) <= self.max_steps:
            raise PlanningError("Plan must contain between 1 and the configured maximum number of steps.")
        steps = []
        seen = set()
        for item in items:
            if not isinstance(item, dict) or set(item) != {
                "step_id", "role", "objective", "acceptance_criteria", "depends_on"
            }:
                raise PlanningError("A plan step does not match the required schema.")
            step_id, role, sub_objective = item["step_id"], item["role"], item["objective"]
            if (
                not isinstance(step_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", step_id)
                or step_id in seen
            ):
                raise PlanningError("Plan step IDs must be unique, lowercase identifiers.")
            if not isinstance(role, str) or role not in _ALLOWED_ROLES:
                raise PlanningError("Plan contains an unsupported specialist role.")
            if not isinstance(sub_objective, str) or not sub_objective.strip() or len(sub_objective) > 4000:
                raise PlanningError("Each plan step needs a bounded, specific objective.")
            raw_criteria = item["acceptance_criteria"]
            if (
                not isinstance(raw_criteria, list)
                or not 1 <= len(raw_criteria) <= self.max_criteria_per_step
                or any(not isinstance(value, str) or not value.strip() or len(value) > 1000 for value in raw_criteria)
            ):
                raise PlanningError("Each plan step needs explicit acceptance criteria.")
            dependencies = item["depends_on"]
            if (
                not isinstance(dependencies, list)
                or any(not isinstance(value, str) or not value for value in dependencies)
                or any(not isinstance(value, str) for value in dependencies)
                or len(set(dependencies)) != len(dependencies)
                or step_id in dependencies
            ):
                raise PlanningError("Plan dependencies must be unique, valid, and non-self-referential.")
            seen.add(step_id)
            steps.append(PlanStep(
                step_id=step_id,
                role=role,
                objective=sub_objective.strip(),
                acceptance_criteria=tuple(value.strip() for value in raw_criteria),
                depends_on=tuple(dependencies),
            ))
        plan_steps = tuple(steps)
        order = self._topological_order(plan_steps)
        # A plan must preserve the user-level acceptance criteria in its work.
        planned_text = " ".join(
            [*(step.objective for step in plan_steps),
             *(criterion for step in plan_steps for criterion in step.acceptance_criteria)]
        ).casefold()
        missing = [criterion for criterion in criteria if criterion.casefold() not in planned_text]
        if missing:
            raise PlanningError("Plan omitted one or more original acceptance criteria.")
        return TaskPlan(objective, criteria, plan_steps, order)

    def plan(
        self,
        objective: str,
        acceptance_criteria: tuple[str, ...] | list[str] = (),
    ) -> TaskPlan:
        if not isinstance(objective, str) or not objective.strip() or len(objective) > 20_000:
            raise ValueError("A non-empty objective of at most 20000 characters is required.")
        if isinstance(acceptance_criteria, (str, bytes)) or not isinstance(acceptance_criteria, (tuple, list)):
            raise ValueError("Acceptance criteria must be a list of strings.")
        if len(acceptance_criteria) > 50 or any(
            not isinstance(value, str) or not value.strip() or len(value) > 2000
            for value in acceptance_criteria
        ):
            raise ValueError("Acceptance criteria must contain at most 50 non-empty strings.")
        criteria = tuple(value.strip() for value in acceptance_criteria)
        prompt = {
            "objective": objective.strip(),
            "acceptance_criteria": list(criteria),
            "max_steps": self.max_steps,
            "allowed_roles": sorted(_ALLOWED_ROLES),
        }
        try:
            response = self.providers.complete(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are BRAIN, the task planning coordinator for a software-building team. "
                            "Break the user's objective into the smallest useful specialist work units. "
                            "Parallelize only genuinely independent tasks. Add explicit depends_on edges "
                            "when a task needs another task's output. Preserve every user constraint. "
                            "Do not add unrelated scope. Return JSON only with exactly this schema: "
                            "{\"steps\":[{\"step_id\":\"requirements\",\"role\":\"requirements-analyst\","
                            "\"objective\":\"specific work\",\"acceptance_criteria\":[\"observable result\"],"
                            "\"depends_on\":[]}]}. Each step must have 1-20 acceptance criteria. "
                            "Use only allowed_roles supplied by the user message. Include QA as a distinct "
                            "step after implementation, and documentation only when relevant."
                        ),
                    },
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
                provider_id=self.provider_id,
                temperature=0.1,
                max_tokens=self.max_tokens,
            )
        except ProviderError:
            raise
        return self._validate(response.content, objective.strip(), criteria)


__all__ = ["PlanStep", "TaskPlan", "PlanningError", "TaskPlanner"]
