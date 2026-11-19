# Existing Component Assessment

**Status:** Initial assessment based on the code shared in earlier conversations  
**Branch:** `development`  
**Important:** The source files for these components are not currently present in this repository. This is a design-level assessment, not a fresh code audit. Reassess the exact source before integration.

## Assessment summary

| Component | Intended responsibility | Current assessment | Integration decision |
|---|---|---|---|
| BRAIN multi-agent core (Python/FastAPI) | Planning, coordination, shared context | Useful prototype direction; persistence, concurrency, validation, limits, and test coverage need work | Adapt as the orchestrator only after source review and contract tests |
| Approval Agent (Python/Ollama) | Decide whether actions need approval | Unsafe to adopt as-is: unrestricted auto mode, shell execution, and an LLM reviewer are not a security boundary | Replace its execution path with deterministic policy enforcement; retain human approval workflow ideas |
| QAEngineer (Node.js) | Inspect project files and run checks | Useful initial QA worker for JavaScript projects; scanning and import detection are limited and need tests | Rework behind a scoped workspace/tool interface; expand language support incrementally |
| ReviewerCritic (Python) | Independent quality review | Heuristic checks can flag patterns but cannot establish correctness or prove tests passed | Keep as a supplemental reviewer, not the sole release gate |
| Deno delegated worker endpoint | Execute a delegated model task and return a structured report | Useful provider-facing worker prototype; currently has no file or command tools | Reuse its task/report boundary; add capabilities only through the policy gateway and sandbox |

## Findings and required safeguards

### 1. BRAIN
- Define and validate a strict task plan schema rather than relying on loosely parsed model output.
- Replace shared JSON-file persistence with transactional storage before concurrent or multi-user operation.
- Add request authentication, bounded session history, timeouts, retries, rate limits, and resource budgets.
- Add automated tests for planning, delegation, failure handling, and session isolation.
- Keep orchestration separate from tool execution and approval enforcement.

### 2. Approval Agent
- Do not allow a model-generated risk score to authorize a dangerous action.
- Do not use unrestricted shell execution or `shell=True`.
- Enforce allowed commands, arguments, paths, network access, and time/resource limits in deterministic code.
- Require explicit human approval for destructive, external, credential-sensitive, deployment, and other high-impact actions.
- Treat autonomous mode as permission-bounded operation, never unrestricted execution.

### 3. QAEngineer
- Resolve paths against a designated workspace root and reject traversal outside it.
- Use an explicit allowlist of scripts and package-manager commands.
- Capture exit codes, stdout/stderr, timeouts, and test artifacts as evidence.
- Make scanning errors visible rather than silently skipping important paths.
- Test import resolution and package-manager detection against real project fixtures.
- Start with JavaScript/TypeScript support; add other ecosystems through separate adapters.

### 4. ReviewerCritic
- Separate objective checks from subjective recommendations.
- Require links to files, line ranges, test output, or other evidence for findings.
- Do not equate keyword absence with correctness or keyword presence with a confirmed defect.
- Make approval depend on explicit acceptance criteria and required QA evidence.
- Keep the reviewer independent from the worker whose changes it evaluates.

### 5. Deno worker endpoint
- Preserve the structured task status contract: completed, blocked, or error.
- Validate input sizes and schema; authenticate callers and protect secrets.
- Add correlation IDs, timeouts, bounded output, and auditable run metadata.
- Do not grant file, shell, or network tools directly to the model.
- Route any future tool request through the policy gateway and an isolated workspace.

## Shared integration contract needed next

Before connecting these components, define versioned schemas for:

1. **Task:** task ID, project ID, objective, context references, constraints, acceptance criteria, assigned worker, and deadline/budget.
2. **Worker result:** status, summary, artifacts, changed files, evidence, blockers, errors, and handoff notes.
3. **Tool request:** tool name, validated arguments, workspace scope, requesting worker, and risk classification.
4. **Approval decision:** pending/approved/rejected, approver, decision time, scope, and reason.
5. **QA report:** checks executed, exact commands, exit codes, output/artifact references, issues, and final status.

All contracts must be validated at runtime. A worker's self-reported status is not proof that an action occurred; the system should verify artifacts and execution evidence independently.

## Integration order

1. Define and test the shared contracts.
2. Build the deterministic policy and approval gateway.
3. Build the isolated workspace/tool adapter.
4. Adapt BRAIN to submit and track contract-compliant tasks.
5. Adapt the Deno worker to the same task/result contract.
6. Integrate QA, repair, and independent review with evidence-based handoffs.
7. Add persistence, dashboard, observability, and deployment.

## Current conclusion

None of these components should be copied into production unchanged. The strongest approach is to reuse their useful ideas and selected code after source-level review, while building shared contracts, deterministic permissions, and isolated execution as the foundation.
