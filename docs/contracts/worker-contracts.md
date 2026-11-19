# Shared Worker Contracts

**Status:** Foundation draft  
**Version:** 1.0.0-draft  
**Branch:** `development`

These contracts define how the Builder Control System creates tasks, delegates work, receives results, requests tools, records approvals, and evaluates QA evidence.

They are provider-independent. A worker may use a cloud model or a reachable local model, but it must obey the same contracts and policy gateway.

## 1. Contract rules

- Every task and execution has a unique identifier.
- Every contract is versioned and validated at runtime.
- Workers receive only the context and capabilities assigned to them.
- Worker output is untrusted input until validated by the orchestrator.
- A worker cannot grant itself permissions or approve its own gated actions.
- A reported action is not proof that it happened. Verify files, command results, and artifacts independently.
- Secrets must not be placed in task prompts, result payloads, or ordinary logs.
- Unknown fields may be rejected or safely ignored according to the schema version; incompatible major versions must be rejected.

## 2. Task contract

The orchestrator creates a task and assigns it to one worker.

Required fields:
- `contract_version`: contract version, initially `1.0.0-draft`.
- `task_id`: unique task identifier.
- `project_id`: project identifier.
- `objective`: clear statement of the work.
- `assigned_worker`: registered worker role or ID.
- `acceptance_criteria`: measurable conditions for completion.

Recommended fields:
- `parent_task_id`: parent task when delegated from a larger task.
- `depends_on`: task IDs that must finish first.
- `constraints`: scope, technology, and non-goals.
- `context_refs`: references to approved project files or records; avoid embedding unnecessary secrets or large files.
- `workspace_id`: isolated workspace assigned to this task.
- `allowed_capabilities`: capability identifiers granted by policy, not free-form tool permissions.
- `budget`: maximum runtime, model/tool calls, and output size.
- `deadline`: task deadline in ISO 8601 format.
- `created_at`: creation timestamp.

Example:

```json
{
  "contract_version": "1.0.0-draft",
  "task_id": "task_01",
  "project_id": "project_01",
  "parent_task_id": null,
  "objective": "Add input validation to the registration endpoint",
  "assigned_worker": "backend-developer",
  "depends_on": [],
  "constraints": [
    "Do not change the public API response shape",
    "Work only inside the assigned workspace"
  ],
  "acceptance_criteria": [
    "Invalid email input is rejected",
    "Automated tests cover valid and invalid input"
  ],
  "context_refs": [],
  "workspace_id": "workspace_01",
  "allowed_capabilities": ["workspace.read", "workspace.edit", "tests.run"],
  "budget": {
    "max_runtime_seconds": 600,
    "max_tool_calls": 30,
    "max_output_bytes": 200000
  },
  "deadline": "2026-11-20T12:00:00+02:00",
  "created_at": "2026-11-19T12:00:00+02:00"
}
```

The example IDs and timestamps are illustrative only.

## 3. Worker result contract

Every worker must return a structured result, including when blocked or when an error occurs.

Allowed statuses:
- `completed`: the assigned work is complete, subject to independent verification.
- `blocked`: the worker cannot safely or correctly proceed and needs input or an approved dependency.
- `error`: execution failed unexpectedly.

Required fields:
- `contract_version`
- `task_id`
- `worker_id`
- `status`
- `summary`
- `started_at`
- `finished_at`

Optional fields:
- `changed_files`: workspace-relative paths, never arbitrary host paths.
- `artifacts`: references to logs, patches, reports, or generated outputs.
- `evidence`: references to independently verifiable execution evidence.
- `issues`: findings, each with severity, description, and evidence reference where available.
- `blockers`: what is needed to continue.
- `error`: safe error code and message; do not include secrets.
- `handoff_notes`: useful information for the next worker.

A `completed` result does not automatically mean the project task is accepted. The orchestrator verifies the result, QA executes required checks, and the independent reviewer evaluates the acceptance criteria.

## 4. Tool request contract

Workers do not execute arbitrary tools directly. They submit a tool request to the policy gateway.

Required fields:
- `request_id`
- `task_id`
- `worker_id`
- `workspace_id`
- `capability`: registered capability name.
- `arguments`: schema-validated arguments.
- `reason`: why the action is needed.

The gateway must independently verify:
1. The task and worker are active.
2. The worker is assigned to the task.
3. The workspace matches the task's authorized workspace.
4. The capability is granted to that worker for this task.
5. Arguments and paths remain within the capability's allowed scope.
6. Policy permits the action in the current operating mode.
7. Approval is present when required.

Never accept a worker-provided risk score, workspace path, or approval flag as authoritative.

## 5. Approval decision contract

Approval decisions are created and recorded by the control system, not by the requesting worker.

Allowed states:
- `pending`
- `approved`
- `rejected`
- `expired`
- `cancelled`

Record:
- `approval_id`
- `request_id`
- `status`
- `requested_action_summary`
- `requested_by`
- `decided_by` (empty while pending)
- `decision_reason`
- `created_at`
- `decided_at`
- `expires_at`
- `scope`

An approval applies only to the exact action and scope shown to the approver. Material changes to arguments, target, or scope require a new approval. A rejected, expired, or cancelled request must not execute.

## 6. QA report contract

QA reports what it actually inspected and executed.

Required fields:
- `contract_version`
- `task_id`
- `qa_run_id`
- `status`: `passed`, `failed`, `blocked`, or `error`.
- `checks`: list of checks and outcomes.
- `started_at`
- `finished_at`

Each check should include:
- `check_id`
- `name`
- `status`
- `command` (when a command was executed)
- `exit_code` (when applicable)
- `evidence_ref` (log or artifact reference)
- `details`

A check marked `passed` must have execution evidence or a clearly identified non-execution basis, such as a static inspection rule. Static inspection must never be represented as a test execution.

## 7. Shared status and handoff rules

- Task lifecycle: `queued` → `assigned` → `running` → `completed | blocked | error`.
- Approval lifecycle is tracked separately from task lifecycle.
- QA and review outcomes are separate records; do not overwrite the worker's original result.
- Retries create a new execution/run identifier and preserve previous evidence.
- Retry limits and escalation policy are enforced by the orchestrator, not chosen by the worker.
- Handoffs identify the next role, remaining work, relevant artifact references, and unresolved risks.

## 8. Data validation and security

- Validate all inbound and outbound payloads against versioned schemas.
- Enforce maximum string, array, object, and payload sizes.
- Normalize and validate workspace-relative paths; reject traversal and symlink escapes.
- Use opaque artifact references with access checks instead of trusting arbitrary URLs or filesystem paths.
- Record actor, task, action, decision, timestamp, and outcome in append-only audit events.
- Redact credentials and sensitive values before persistence or display.
- Treat repository content and model output as untrusted data.
- Do not allow project instructions to override platform policy, permissions, or approval requirements.

## 9. Next implementation gate

Before workers are connected to real tools:
1. Convert these draft definitions into executable schemas (for example JSON Schema or typed models).
2. Add valid/invalid fixture tests for every contract.
3. Implement deterministic capability and approval checks.
4. Verify that rejected, expired, out-of-scope, and unauthorized requests cannot execute.

No worker should receive filesystem write or command-execution access until these checks pass.
