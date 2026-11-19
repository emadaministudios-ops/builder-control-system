# Worker execution and QA supervision

This document describes the current development-stage execution components. It is not a production deployment guide.

## BRAIN planning and dependency-aware execution

The runtime exposes `plan_and_create_tasks(objective, acceptance_criteria)`. BRAIN asks the configured model to split the request into bounded specialist steps. Deterministic validation rejects unsupported roles, malformed steps, missing acceptance criteria, unknown dependencies, duplicate IDs, and dependency cycles. The validated plan is persisted as individual tasks with explicit prerequisite IDs.

The authenticated API exposes the same workflow through `POST /v1/plans`. Only a human principal with the `tasks:create` scope may submit a top-level plan.

Run a validated plan with:

```python
plan, records = runtime.plan_and_create_tasks(
    "Build the requested feature",
    ["The existing login flow remains intact."],
)
result = runtime.scheduler.dispatch_plan(
    [record.task.task_id for record in records]
)
```

The scheduler executes dependency-ready waves, parallelizing independent tasks up to the configured concurrency ceiling. The orchestrator independently checks prerequisites at dispatch time, so a direct dispatch call cannot bypass the dependency gate. If a prerequisite fails, is blocked, or has no available worker, its dependants remain queued rather than being treated as complete. When a prerequisite completes, BCS passes its verified task ID, objective, specialist role, result summary, changed-file list and evidence references into the dependent worker's task context. This gives downstream agents the actual outputs they need instead of asking them to rediscover prior work.

The scheduler does not yet infer file-level conflicts or automatically allocate separate Git worktrees. The trusted workspace factory remains responsible for ensuring concurrently executing tasks do not mutate the same workspace.

## Model-backed specialist workers

`BCSRuntime.register_model_worker(...)` registers a role-specific worker using the runtime's configured provider registry. The caller must supply a trusted `workspace_factory(task)` that returns the isolated `Workspace` assigned to that task.

Example:

```python
from pathlib import Path
from bcs.policy import OperatingMode
from bcs.runtime import create_runtime
from bcs.workspace import Workspace

runtime = create_runtime()
runtime.register_model_worker(
    worker_id="frontend-worker-1",
    role="frontend-developer",
    workspace_factory=lambda task: Workspace(
        workspace_id=f"workspace-{task.task_id}",
        root=Path("/srv/bcs/workspaces/project-a"),
    ),
    mode=OperatingMode.AUTONOMOUS,
)
```

This example uses a trusted workspace root supplied by the application. A real deployment must allocate a distinct worktree or directory for each concurrently active task; do not point independent workers at the same mutable directory.

The worker:
- Sends the original objective and acceptance criteria to the selected model.
- Requires a strict JSON proposal.
- Enforces file-count and content-size limits.
- Sends every proposed file write through `WorkspaceToolGateway`.
- Cannot execute shell commands, access secrets, or grant itself capabilities.
- Returns blocked when policy denies a write or a human approval is pending.

Current limitation: the worker does not yet have a complete iterative tool loop for reading arbitrary project context, running tests, receiving QA findings and repairing automatically. It currently accepts structured file-edit proposals. Do not interpret a model response as proof that code works.

## Bounded parallel scheduling

`runtime.scheduler.dispatch_independent(task_ids)` dispatches a caller-selected batch concurrently up to the configured `max_concurrency` ceiling. The default is two.

Only submit tasks known to be independent. The current scheduler does not yet calculate a dependency graph or detect overlapping file ownership. The workspace factory must provide separate task workspaces. Queue size and concurrency are operational safeguards, not user-facing usage tiers or credits.

## Three-pass QA supervisor

Create a read-only QA supervisor with a trusted workspace factory:

```python
supervisor = runtime.create_qa_supervisor(
    workspace_factory=trusted_qa_workspace_factory,
)
review, report, feedback, repair_task_ids = runtime.review_and_handoff(
    worker_task,
    worker_result,
    supervisor,
)
```

The supervisor performs three model-assisted passes:
1. Derives observable requirements from the original objective and acceptance criteria.
2. Compares the worker's changed files against every requirement and proposes actionable findings.
3. Audits the initial critique against the original objective to look for missed requirements, false positives and weak repair guidance.

The supervisor is read-only. It cannot modify the implementation, run tests, approve releases or declare a test pass. A review with no findings is marked `ready_for_tests`, not passed. The generated QA report explicitly marks these checks as non-executed static review.

When findings exist, BCS persists the feedback cycle and creates specialist repair tasks through the deterministic QA handoff rules. Those repair tasks are queued; automatic dispatch, retest and repeated repair orchestration remain future work.

## Provider configuration

Configure one or both providers through environment variables:

- Local Ollama: `BCS_OLLAMA_MODEL` and optional `BCS_OLLAMA_BASE_URL` (default `http://127.0.0.1:11434/v1`).
- Cloud: `BCS_CLOUD_MODEL`, `BCS_CLOUD_BASE_URL`, and `BCS_CLOUD_API_KEY_ENV` (defaults to `OPENAI_API_KEY`).
- Default selection: `BCS_DEFAULT_PROVIDER` (for example `ollama` or `cloud`).

BCS does not silently fall back to another provider. Local inference avoids per-request cloud API billing, but requires sufficient compute and still has hardware/electricity costs. Cloud provider charges, rate limits and context limits remain outside BCS control.

## Verification boundary

CI uses mocked model responses and temporary workspaces. It verifies contracts, policy enforcement, routing, feedback state and concurrency ceilings. It does not prove that a particular real model can solve complex projects, that Docker is installed and running, or that a cloud deployment is secure and available.
