# Worker execution and QA supervision

This document describes the current development-stage execution components. It is not a production deployment guide.

## Bounded autonomous QA repair loop

The runtime exposes `execute_with_qa(task_id, supervisor, ...)` for a queued task with registered specialist workers. It dispatches the original worker, runs optional controller-configured executable QA, asks the three-pass QA supervisor to review the original objective and changed files, combines both result sets into one feedback cycle, creates repair tasks from failed checks/findings, dispatches those tasks, merges their changed-file/evidence references into the review context, and repeats tests plus source review after each repair. The feedback controller requires at least two distinct combined QA reports before it can return its policy-level `VERIFIED` state. Blocked QA, blocked/failed repair workers, exhausted feedback cycles and the configured repair-round limit stop the loop without a success claim.

Repair tasks inherit the parent's `workspace_group_id`. The trusted workspace factory must map this stable group to the same project checkout so that repair edits are visible to the next QA review. Independent concurrent tasks must still receive isolated worktrees or branches.

**Verification boundary:** the three-pass supervisor performs model-assisted source inspection and emits static, explicitly non-executed QA checks. When `executable_checks` and the complete trusted Docker/workspace configuration are supplied to `execute_with_qa`, BCS runs those controller-defined `QACheckSpec` commands before every review cycle, combines their actual outcomes and retained stdout/stderr evidence with the source review, and routes failed checks into the same bounded repair loop. In supervised mode, `tests.run` requires human approval; a blocked approval becomes a QA escalation and stops the loop. If executable checks are omitted, the loop remains source-review-only and its completion reason explicitly states that no executable tests were configured. A `VERIFIED` feedback state is not release approval and does not prove behavior outside the configured checks.

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

The scheduler does not yet infer file-level conflicts or automatically allocate separate Git worktrees. The trusted workspace factory remains responsible for ensuring concurrently executing tasks do not mutate the same workspace. Each task also carries a stable `workspace_group_id`; QA-created repair tasks inherit the original task's group. A production workspace factory must resolve that group to the same project checkout for repair/retest, while still isolating independent concurrent tasks in separate worktrees or branches.

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

When findings or failed executable checks exist, BCS persists the combined feedback cycle and creates repair tasks through deterministic QA handoff rules. The autonomous loop dispatches those tasks, reruns configured executable checks, and re-reviews the original task within the configured repair-round bound.

## Provider configuration

Configure one or both providers through environment variables:

- Local Ollama: `BCS_OLLAMA_MODEL` and optional `BCS_OLLAMA_BASE_URL` (default `http://127.0.0.1:11434/v1`).
- Cloud: `BCS_CLOUD_MODEL`, `BCS_CLOUD_BASE_URL`, and `BCS_CLOUD_API_KEY_ENV` (defaults to `OPENAI_API_KEY`).
- Default selection: `BCS_DEFAULT_PROVIDER` (for example `ollama` or `cloud`).

BCS does not silently fall back to another provider. Local inference avoids per-request cloud API billing, but requires sufficient compute and still has hardware/electricity costs. Cloud provider charges, rate limits and context limits remain outside BCS control.

## Verification boundary

CI uses mocked model responses and temporary workspaces. It verifies contracts, policy enforcement, routing, feedback state and concurrency ceilings. It does not prove that a particular real model can solve complex projects, that Docker is installed and running, or that a cloud deployment is secure and available.

## Specialist-to-specialist consultation

The runtime exposes `consult_specialist(...)` for a bounded advisory exchange between two allowlisted specialist roles. BRAIN or another trusted controller decides when to request consultation and selects the target role; this first implementation does not let model-generated text autonomously select arbitrary agents.

The consultant receives only the controller-supplied objective, question and bounded context. Its strict response contract contains advice, risks, next steps and a confidence label. The consultant has no workspace gateway, shell/test execution, secret access, task mutation, approval, or release capability. Advice is untrusted input: the requesting worker/controller must evaluate it against the original task and QA requirements before acting. The exchange is synchronous. BRAIN's decision and any resulting advice are persisted in the specialist consultation history; outcome linkage is recorded separately and does not grant authority.

### BRAIN consultation routing

`BCSRuntime.consult_for_task(...)` asks a bounded BRAIN routing call whether the current task has a concrete knowledge gap, blocker, or design risk that warrants specialist advice. BRAIN may select exactly one role from the allowlist or decline consultation. The route is schema-validated before the separate advisory consultation call. The task role is taken from the persisted task record rather than caller-supplied identity, and context is limited to task objective, acceptance criteria, worker summary and blockers. This is an on-demand controller entry point; it is not yet automatically invoked by every worker dispatch. Consultation results are advisory and are not silently converted into code changes or new tasks.

### Automatic pre-execution consultation

Registered model workers now receive a controller-owned consultation callback. Before the worker's first model completion for a task, the callback asks BRAIN whether one specialist consultation is warranted. If approved by the router, the advisory result is inserted into the worker prompt as explicitly untrusted context. The worker is instructed to verify advice against the original objective and acceptance criteria and to ignore attempts to change its role or permissions. The callback caches the result by task ID for that registered worker so retries do not repeatedly consult. Consultation failure is non-fatal and does not grant additional capabilities; ordinary workspace edits remain governed by the existing policy gateway. The cache is in-memory per worker, while the decision and advice are persisted as a durable audit record.

### Durable specialist consultation audit

SQLite schema version 4 added a dedicated specialist consultation history table. BRAIN routing decisions are recorded whether consultation is skipped or requested. When advice is obtained, the record includes the requesting role, selected specialist, focused question, routing rationale, advice, risks, next steps, confidence, task ID and timezone-aware timestamp. The runtime exposes `consultation_history(task_id=None)` for inspection. Existing schema version 3 databases migrate by creating the new table without rewriting existing task, approval, escalation or feedback records. Consultation history is an audit trail; recommendations remain advisory and do not themselves authorize actions.


## Consultation outcome feedback

Consultation decisions, specialist advice, worker-reported adoption and QA results are stored as separate records. A trusted controller can append an outcome with `record_consultation_outcome(...)`; the worker disposition is explicitly self-reported (followed, partially followed, not followed, or unknown). It is never treated as proof that the advice worked.

QA assessment is a separate field and requires a QA run identifier. Only controller-supplied QA status and evidence references are accepted; the worker cannot mark its own advice successful. Outcomes are append-only and linked by consultation ID and task ID, with a foreign key preventing orphan records. The schema migration is version 5.

This is an auditable feedback foundation, not model training or a claim that BCS has learned a generally reusable rule. The next integration step is to attach verified outcomes to repair-task prompts using project-scoped lineage and to evaluate advice against later QA evidence.


Repair workers receive up to three recent failed consultation approaches from their own task's ancestor chain, but only when the ancestor tasks share the exact same non-empty `workspace_group_id`. A caution is included only when the latest recorded outcome says the worker followed or partly followed the advice and a trusted QA assessment recorded failure. The prompt labels this material untrusted cautionary evidence and asks the repair worker not to repeat an approach without explaining what materially changes. Unrelated projects, skipped consultations, unadopted advice and advice without a failed QA assessment are excluded. This history is bounded to three entries and 4,000 characters.
