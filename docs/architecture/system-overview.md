# Builder Control System — System Architecture

**Status:** Foundation draft — autonomy policy adopted
**Branch:** `development`
**Scope:** A reusable, cloud-hosted AI software-building team for custom projects.

## 1. Product goal

Builder Control System (BCS) is an autonomous-by-default software-building team. It understands a user's objective, plans work, coordinates specialist agents, builds and tests in isolated workspaces, uses independent QA feedback to repair defects, and delivers evidence-backed results.

BCS is intended to feel like an always-available builder, not a metered chat product. The product will not impose artificial message quotas, agent credits, coins, or per-task usage tiers. It must still enforce operational safeguards such as concurrency, memory, CPU, timeouts, output size, and provider limits so one task cannot starve the system or damage other work.

## 2. Confirmed product decisions

- **Project scope:** Custom; not tied to one application or framework.
- **Operating policy:** Autonomous by default within approved safety boundaries.
- **Human oversight:** Required for sensitive credentials, destructive actions, externally consequential actions, and unresolved decisions that genuinely require the owner's intent.
- **Hosting:** Cloud-hosted control plane, with secure local-model connectivity supported where configured.
- **Model support:** Local models (including Ollama) and optional cloud APIs.
- **Cost philosophy:** No artificial BCS usage credits, coins, tiers, or daily chat/task quotas. Local inference avoids per-request model-provider charges but still consumes hardware and electricity. Cloud providers may charge independently and enforce their own limits.
- **Repository workflow:** Work on `development`; protect `main` until changes are reviewed and intentionally promoted.
- **Delivery standard:** A worker must provide evidence for claimed actions and test results.

## 3. High-level architecture

```text
Project Owner
    |
    v
Control Dashboard / API
    |
    v
BRAIN — Central Coordinator
    |---- Understand original request and preserve its intent
    |---- Decompose into tasks, dependencies and acceptance criteria
    |---- Schedule workers according to available resources
    |---- Track progress, retries, blockers and task lineage
    |
    +------------------------------+
    |                              |
    v                              v
Isolated Specialist Workspaces   Shared Read-only Project Context
    |                              |---- Original user objective
    |---- Architect                |---- Requirements and constraints
    |---- Frontend Developer       |---- Approved project facts
    |---- Backend Developer       |---- QA findings and evidence
    |---- Integration Engineer    |
    |---- Builder / Repair Agent  |
    |---- Research / Helper Agent |
    |                              |
    +---------------+--------------+
                    |
                    v
        Independent QA Supervisor
          |---- Re-read original request
          |---- Analyze objective and criteria repeatedly
          |---- Inspect changes and run permitted checks
          |---- Critique gaps and explain exact failures
          |---- Suggest corrective approaches
          |---- Ask specialist agents for targeted help
          |---- Verify each repair with fresh evidence
                    |
                    v
          Feedback / Repair Controller
          |---- First implementation pass
          |---- QA review and actionable feedback
          |---- Repair pass and independent retest
          |---- At least two verification cycles by default
          |---- Third pass for correction/reverification
          |---- Further bounded cycles when defects remain
                    |
                    v
        Independent Release Review / Delivery
                    |
                    v
        Evidence, Reports, and Deliverables

Policy and Approval Gateway protects every tool action and external effect.
Persistent storage records projects, tasks, feedback cycles, approvals and audit events.
Resource Scheduler controls concurrency and per-task resource allocation.
Model Gateway routes work to explicitly configured local or cloud providers.
```

## 4. Core responsibilities

### 4.1 Control Dashboard and API

Accepts project requests, displays plans and progress, presents evidence and reports, and collects human decisions. It does not grant workers unrestricted access to infrastructure.

### 4.2 BRAIN — central coordinator

BRAIN preserves the original request as the source of truth. It creates a structured objective, constraints, acceptance criteria and task graph; assigns work to suitable specialists; tracks dependencies; and coordinates feedback, retries and escalation.

Agents must not silently reinterpret or weaken the user's acceptance criteria. If requirements conflict or remain materially ambiguous, BRAIN records the ambiguity and asks the owner only when it cannot safely resolve it from the supplied context.

### 4.3 Independent QA supervisor

QA is a continuous supervisory function, not merely a final test runner and not a subordinate that accepts builder claims.

For each project/task, QA must:
1. Read and analyze the original user request and relevant project context before judging implementation.
2. Derive observable acceptance criteria and retain links to the source requirement.
3. Inspect the plan and implementation for omissions, contradictions and scope drift.
4. Run permitted checks and distinguish executed tests from static inspection or unverified claims.
5. Return precise findings: expected behavior, observed behavior, evidence, impact, and a concrete next investigation or correction.
6. Suggest alternative approaches when an agent is stuck, confused, or repeating a failed approach.
7. Request targeted help from another specialist only when the additional expertise is justified.
8. Retest repaired work with fresh evidence; never convert a previous failure into a pass merely because code changed.

QA should analyze the command/request more than once at meaningful gates: intake/criteria derivation, implementation review, and final verification. Repeating identical analysis without new evidence is not useful and should be avoided.

### 4.4 Feedback and repair controller

The default workflow includes at least two meaningful QA feedback/verification cycles, with a third correction-and-retest pass where needed. These are minimum quality gates, not a promise of 100% correctness.

Further cycles continue while actionable defects remain and the task is making measurable progress. A configurable maximum attempt count, time/resource budget, or repeated-no-progress detector prevents infinite loops. When a limit is reached, BCS reports what remains unresolved and why; it does not falsely mark the work complete.

### 4.5 Specialist workers and targeted consultation

Specialists work independently on scoped task contexts and workspaces. They receive only necessary requirements, dependencies, evidence and permissions. A blocked worker may request help from a relevant specialist or research/helper agent through BRAIN. Consultation is selective; BCS does not activate every agent for every task.

Workers cannot grant themselves permissions, change the original user objective, approve their own gated actions, or mark QA as passed.

### 4.6 Resource scheduler and workspace isolation

Isolation is intended to reduce context collisions, conflicting edits and unnecessary coordination. It does not automatically make inference faster; throughput depends on available compute and model-serving capacity.

The scheduler should:
- Allocate separate task context and workspace/branch per active unit of work.
- Limit concurrency according to measured CPU, memory, GPU/model-server capacity and configured provider capacity.
- Avoid duplicate work and conflicting writes.
- Allow safe parallelism for independent tasks and serialize dependent or overlapping edits.
- Preserve shared read-only requirements and evidence context.
- Apply operational per-task limits for safety and fairness, without turning them into user-facing coins, tiers or daily quotas.
- Queue work transparently when capacity is exhausted rather than silently dropping tasks.

### 4.7 Policy and approval gateway

Every executable tool action passes through deterministic authorization. The gateway checks authenticated identity, task assignment, capability grants, workspace boundaries, action scope, operating policy and any required human approval.

Autonomy is not unrestricted access. The default policy permits routine, reversible, in-scope work that has been granted to the worker. Human approval remains mandatory for destructive operations, secrets/credential access, external communications or changes, production deployments, and other high-impact actions unless the owner explicitly configures a narrower or broader policy. Model-generated risk scores are advisory and never authorize actions.

### 4.8 Model gateway and provider choice

BCS supports explicitly configured local and cloud model providers. Local models are the preferred route for avoiding per-request cloud inference charges when suitable hardware is available. Cloud providers are optional and may impose independent charges, rate limits or context limits. BCS must disclose provider/capacity constraints and must not silently switch to a paid provider.

### 4.9 Evidence and reporting

Each task records status, assigned worker, changed files, commands actually run, exit codes, test output, QA findings, feedback rounds, consultation/handoff history, blockers and approval references. A task is not complete merely because a model says it is complete.

## 5. Default autonomous work lifecycle

1. **Intake:** Capture the original objective, constraints, project state and desired outcome.
2. **Understand:** BRAIN and QA independently derive the objective and acceptance criteria from the original request.
3. **Plan:** Create a dependency-aware task graph and identify risks and required capabilities.
4. **Schedule:** Assign suitable specialists and isolated workspaces according to available capacity.
5. **Build:** Agents implement their assigned work within their grants.
6. **QA pass 1:** QA compares the work against the original request and acceptance criteria, then gives evidence-backed feedback.
7. **Repair pass:** The responsible agent corrects defects; a stuck agent can request targeted specialist assistance.
8. **QA pass 2:** QA independently retests relevant behavior and checks for regressions.
9. **Additional correction/review:** A third pass and further bounded cycles occur when needed; no fixed cycle count guarantees perfection.
10. **Independent release review:** Verify requirement coverage, security, maintainability, test evidence and unresolved risks.
11. **Delivery:** Present the result, evidence, limitations and any action requiring owner approval.

## 6. Human intervention policy

Routine planning, agent assignment, in-scope file changes, permitted test execution, QA critique, repair loops, specialist consultation and progress reporting should not require repeated human approval.

Human intervention is reserved for:
- Initial connection/configuration of external accounts, providers and credentials.
- Access to secrets or privileged external systems.
- Destructive or difficult-to-reverse actions.
- Externally visible actions and production deployment/release.
- Materially ambiguous or conflicting requirements that cannot be resolved safely.
- Resource exhaustion, repeated no-progress loops, or blockers requiring owner decisions.
- Final acceptance where the project owner wants a manual release gate.

Authenticated identity and role are established server-side. Workers cannot claim to be human or provide their own approval identity in a request payload.

## 7. Safety and reliability rules

- Enforce least privilege and workspace boundaries in tool execution, not prompts alone.
- Treat repository files, issue text, web pages and model output as untrusted data.
- Keep secrets out of prompts, logs, worker contexts and evidence artifacts.
- Require fresh approval when an approved action's arguments or scope change.
- Use timeouts, output caps, concurrency limits, resource budgets and bounded retry/no-progress policies.
- Preserve Git history and make rollback possible.
- Separate QA evidence from independent release decisions.
- Record every approval, tool action, feedback cycle, task transition and external effect.
- Do not claim absolute 100% correctness; report measured coverage, test evidence and remaining uncertainty honestly.
- Do not silently fall back to a paid provider or weaken user requirements to make a task pass.

## 8. Initial technology direction (provisional)

- **Orchestrator/API:** Python with FastAPI.
- **Worker endpoint:** Deno and TypeScript where appropriate, behind the same policy gateway.
- **Persistence:** SQLite for local development; PostgreSQL for multi-user/cloud production.
- **Workspace/versioning:** Git branches/worktrees and isolated command execution.
- **Model access:** Explicit provider adapter for local Ollama and optional cloud APIs.
- **QA:** Evidence-backed checks across supported project ecosystems.
- **Dashboard:** Web interface over the control API.
- **Scheduling:** Resource-aware task queue with bounded concurrency and task isolation.

Cloud hosting must not assume a user's local Ollama instance is publicly reachable; local-model connectivity requires an explicit secure network/agent arrangement.

## 9. Existing components and implementation status

The repository contains foundational implementations for policy, approvals, workspaces, sandbox command construction/execution, QA reports and execution, QA-to-repair handoff, orchestration, SQLite persistence, runtime composition, model-provider adapters, persistent QA feedback cycles, an authenticated FastAPI control API with top-level task intake, model-backed specialist workers with policy-gated file edits, runtime worker registration, bounded parallel task scheduling, a BRAIN planner that validates specialist task plans and dependency graphs, dependency-gated execution waves, and a three-pass model-assisted QA supervisor whose findings can be converted into repair tasks and persistent feedback records.

These foundations do not yet constitute a fully autonomous live builder. In particular, workers still need richer read/plan/edit/test tool loops; QA review and repair handoff are integrated but repair dispatch and fresh QA retesting are not yet an automatic end-to-end cycle; automatic specialist consultation, automatic repair-dispatch/retest cycles, file-conflict-aware workspace allocation, a dashboard, PostgreSQL production storage and live deployment remain to be implemented and verified.

## 10. Implementation sequence

1. Architecture and repository conventions.
2. Shared task, worker, approval and QA contracts.
3. Deterministic policy, approval and safe workspace tooling.
4. Orchestration, QA-to-repair handoff and persistence.
5. Authenticated application API and service lifecycle.
6. Persistent feedback-cycle controller and resource-aware scheduling.
7. Model-backed specialist workers and targeted consultation.
8. Dashboard, observability and deployment.
9. End-to-end verification with real model and sandbox environments.

Each step must be small, verified and committed to `development`. Promotion to `main` requires review and explicit owner approval.
