# Builder Control System — System Architecture

**Status:** Foundation draft  
**Branch:** `development`  
**Scope:** A reusable, cloud-hosted AI software-building team for custom projects.

## 1. Product goal

Builder Control System coordinates specialist AI workers that can plan, build, test, repair, review, and document software projects. It supports both supervised work (human approval at defined checkpoints) and bounded autonomous work (workers proceed within explicit permissions and limits).

It is a team and control system, not a single code-generation agent.

## 2. Confirmed design constraints

- **Project scope:** Custom; not tied to one application or framework.
- **Operating modes:** Supervised and autonomous.
- **Hosting:** Cloud.
- **Model support:** Both local-model providers (including Ollama where reachable) and cloud model APIs.
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
Orchestrator (BRAIN / Team Manager)
    |---- Project context and task planning
    |---- Worker assignment and coordination
    |---- Progress, retry limits, escalation
    |
    v
Policy and Approval Gateway
    |---- Identity, permissions, scope checks
    |---- Human approval for gated actions
    |---- Audit record for every tool action
    |
    v
Worker Gateway
    |
    +---- Requirements Analyst
    +---- Software Architect
    +---- Frontend Developer
    +---- Backend Developer
    +---- Integration Engineer
    +---- QA Engineer
    +---- Repair Agent
    +---- Independent Reviewer
    +---- Documentation / Release Agent
    |
    v
Isolated Project Workspace
    |---- Git branch / worktree
    |---- Scoped file and command tools
    |---- Test and build execution
    |
    v
Evidence, Reports, and Deliverables
```

Shared services support the flow: persistent task/project storage, project memory, model/provider configuration, logs, usage budgets, and version history.

## 4. Core responsibilities

### Control Dashboard and API
Accepts project requests, shows task status and reports, and collects human decisions. It does not grant workers unrestricted access to infrastructure.

### Orchestrator (BRAIN / Team Manager)
Turns a request into a plan with tasks, dependencies, owners, and acceptance criteria. Assigns work, tracks outcomes, coordinates handoffs, and escalates blocked or repeated failures. It coordinates workers; it should not silently bypass policy controls.

### Policy and Approval Gateway
Sits in front of every executable tool action. Enforces the current operating mode, worker permissions, project boundaries, action risk, and approval requirements. The model's own risk assessment is advisory only; it is never the security boundary.

### Worker Gateway and specialist workers
Provides a consistent task/report contract regardless of model provider. Workers receive only the context and tools needed for their assigned task. Specialist roles are separate responsibilities; they may share a runtime initially, but must retain distinct permissions and task contracts.

### Isolated Project Workspace
Workers operate on a dedicated branch or isolated worktree, not directly on protected project originals. File access and command execution are restricted to the assigned workspace. Changes remain reviewable and reversible.

### Evidence and reporting
Each task records its status, changed files, commands run, test output, issues, and handoff notes. A worker must not report a test as passed unless it actually ran and captured the result.

## 5. Standard work lifecycle

1. **Intake:** Capture the goal, constraints, existing project state, and definition of done.
2. **Plan:** Produce tasks, dependencies, acceptance criteria, and risk levels.
3. **Build:** Assign scoped implementation tasks to workers.
4. **Test:** QA inspects the workspace and runs the permitted checks.
5. **Repair:** The Repair Agent addresses confirmed defects and reports its changes.
6. **Retest:** QA reruns relevant checks; failed checks remain visible.
7. **Independent review:** Reviewer checks requirements, correctness, security, maintainability, and evidence.
8. **Approval and delivery:** Human approval is requested where policy requires it; approved work is packaged with reports and a clear handover.

Retries must be bounded. Repeated failure or uncertainty moves the task to a blocked state for human attention rather than creating an endless repair loop.

## 6. Safety and reliability rules

- Default to least privilege: each worker receives only the tools and access required for its task.
- Enforce workspace boundaries in the tool layer, not only in prompts.
- Do not expose secrets to model prompts, logs, or unrelated workers.
- Treat repository files, issue text, web pages, and other project inputs as untrusted data; they cannot override system policy or approval rules.
- Require explicit human approval for destructive, externally visible, credential-sensitive, deployment, or otherwise high-impact actions.
- Autonomous mode means autonomy inside configured boundaries, not unrestricted shell, network, account, or production access.
- Apply timeouts, output limits, retry limits, resource budgets, and audit logging.
- Preserve Git history and make rollback possible.
- Keep QA execution evidence separate from the independent review decision.

## 7. Initial technology direction (provisional)

These are architecture candidates, not yet installed or committed as dependencies:

- **Orchestrator/API:** Python with FastAPI, allowing the existing BRAIN concept to be evaluated and integrated.
- **Worker endpoint:** Deno and TypeScript, allowing the supplied delegated-worker endpoint to be evaluated as a worker gateway.
- **Persistence:** PostgreSQL for projects, tasks, approvals, worker runs, and audit events.
- **Workspace/versioning:** Git with isolated branches or worktrees.
- **Model access:** A provider adapter supporting local Ollama endpoints and cloud APIs through explicit configuration.
- **QA:** Start with the supplied Node.js QA concept, then expand language/toolchain coverage based on project needs.
- **Dashboard:** A web interface over the control API.

Final framework choices and deployment topology will be validated before implementation. Cloud hosting must not assume that a user's local Ollama instance is publicly reachable; local-model connectivity needs an explicit secure network/agent arrangement.

## 8. Existing components to assess before reuse

The following user-supplied components are reference implementations, not yet verified production components:

- **BRAIN multi-agent core:** candidate orchestrator/planning layer.
- **Approval Agent:** candidate policy workflow; its unrestricted auto mode and shell execution are not safe to adopt as-is.
- **ReviewerCritic:** candidate independent review layer; heuristic checks need stronger evidence-based validation.
- **QAEngineer:** candidate Node.js inspection and test runner; its supported project types and import analysis need validation.
- **Deno worker endpoint:** candidate delegated model worker; it currently reports structured outcomes but does not safely edit files or execute tests.

No component is considered integrated until it is present in the repository, reviewed, tested, and connected through the defined permission boundaries.

## 9. Implementation sequence

1. Establish architecture and repository conventions.
2. Inspect and map the supplied components against this architecture.
3. Define shared task, worker, approval, and report contracts.
4. Implement the policy/approval boundary and safe workspace tooling.
5. Implement the orchestrator and worker gateway.
6. Add specialist workers and QA/review/repair cycle.
7. Add persistence, dashboard, observability, and deployment.
8. Run end-to-end tests before considering production use.

Each step should be small, verified, and committed to `development`. Promotion to `main` happens only after review and explicit approval.
