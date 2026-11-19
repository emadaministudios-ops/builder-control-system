# BCS API setup (development)

The control API is a FastAPI application. It exposes health, top-level task intake, task status, approval decisions, QA feedback, integration history and human-gated integration promotion controls.

## 1. Install dependencies

From the repository root:

```bash
python -m pip install -r requirements.txt
```

The package uses a `src` layout. Set `PYTHONPATH=src` before starting the server.

## 2. Create a bearer token

Generate a high-entropy token locally and store the plaintext in your password manager or secret manager. Do not commit it, paste it into issues, or add it to repository files.

Example token generation:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

BCS stores only the SHA-256 digest in its API configuration. Calculate the digest locally:

```bash
python -c "import getpass,hashlib; token=getpass.getpass('Paste token: '); print(hashlib.sha256(token.encode()).hexdigest())"
```

Configure `BCS_API_TOKEN_HASHES` as a JSON array. Example shape (replace the digest and identity with your own values):

```json
[
  {
    "token_sha256": "REPLACE_WITH_64_CHARACTER_SHA256_HEX",
    "actor_id": "project-owner",
    "actor_type": "human",
    "scopes": [
      "tasks:read",
      "tasks:create",
      "tasks:execute",
      "approvals:read",
      "approvals:decide",
      "integrations:read",
      "integrations:promote"
    ]
  }
]
```

Worker tokens should use `actor_type: "worker"` and only the narrow scopes needed by their integration. A worker token cannot decide approvals or create owner-level tasks, even if a request body claims a human identity. Identity and actor type come exclusively from this server-side configuration.

Available scopes currently used by the API:
- `tasks:read`
- `tasks:create`
- `tasks:execute` (human principals only)
- `approvals:read`
- `approvals:decide`
- `integrations:read`
- `integrations:promote` (human principals only)

## 3. Start the development server

Set `BCS_DATABASE_PATH` to a writable SQLite path and `BCS_API_TOKEN_HASHES` to the JSON configuration above. Then run:

```bash
PYTHONPATH=src python -m uvicorn bcs.api:create_app --factory --host 127.0.0.1 --port 8000
```

On Windows PowerShell, set environment variables with `$env:NAME = "value"` and start the same Uvicorn command.

The default bind address is loopback for local development. For cloud hosting, put the service behind a TLS-terminating reverse proxy, restrict network access, and inject token hashes through the deployment secret/configuration manager. Do not expose an unencrypted API directly to the public internet.

## 4. Current API routes

| Method | Route | Authentication |
|---|---|---|
| GET | `/health` | Public, minimal status only |
| POST | `/v1/tasks` | Human principal with `tasks:create` |\n| POST | `/v1/plans` | Human principal with `tasks:create` |
| POST | `/v1/builds` | Human principal with `tasks:create` and `tasks:execute` (returns HTTP 202) |\n| POST | `/v1/tasks/dispatch` | Human principal with `tasks:execute` (returns HTTP 202) |\n| GET | `/v1/execution-jobs` | `tasks:read` (own jobs only) |\n| GET | `/v1/execution-jobs/{job_id}` | `tasks:read` (owner only) |
| GET | `/v1/tasks` | `tasks:read` |
| GET | `/v1/approvals` | `approvals:read` |
| POST | `/v1/approvals/{request_id}/decision` | Human principal with `approvals:decide` |
| GET | `/v1/feedback/{task_id}` | `tasks:read` |\n| GET | `/v1/integrations` | `integrations:read` |\n| GET | `/v1/integrations/{integration_id}/qa` | `integrations:read` |
| POST | `/v1/integrations/{integration_id}/qa` | Human principal with `qa:execute`; server-owned profile only |\n| GET | `/v1/integration-promotions` | `integrations:read` |\n| POST | `/v1/integrations/{integration_id}/promotion-requests` | Human principal with `integrations:promote` |\n| POST | `/v1/integration-promotions/{request_id}/execute` | Human principal with `integrations:promote`, after approval |

When a trusted Git manager and at least one model provider are configured, the app factory registers the standard specialist roles against isolated Git worktrees. QA execution output is persisted as immutable evidence artifacts in the same SQLite database, with captured stdout/stderr, byte size and SHA-256 integrity verification. Integration QA history keeps the evidence references bound to the exact integration commit. The default artifact limit is 200 KB (maximum configurable size 2 MB); production deployments still need encrypted durable backups, retention controls and suitable access management.

For a single-request build, `POST /v1/builds` accepts the original objective and acceptance criteria, asks BRAIN to create the dependency plan, persists its tasks, and enqueues the complete plan as one durable job. It returns the plan, task IDs, job ID and polling URL. This route requires both `tasks:create` and `tasks:execute` and a human principal.

A human can submit the task IDs returned by `/v1/plans` to `/v1/tasks/dispatch`; the API validates task existence, persists an execution job, and returns HTTP 202 with a job ID. A background runner claims queued jobs using expiring SQLite leases and runs dependency-ready waves. When a trusted Git manager and model provider are configured, the runner also performs three-pass source QA on completed specialist tasks, creates bounded repair tasks from findings, and reviews repairs. Job outcomes expose each task's QA status. Without the trusted Git manager/provider, BCS runs dispatch only and QA remains unconfigured. Automatic source review does not run project tests; executable QA still requires separately configured controller-defined sandbox checks. Poll `/v1/execution-jobs/{job_id}` or list the caller's jobs at `/v1/execution-jobs`. Jobs are scoped to the authenticated requester. `BCS_WORKER_MODE` selects `autonomous` (default) or `supervised`; workers only receive workspace read/edit capabilities. If no provider or Git manager is configured, no default workers are registered. SQLite is a development persistence adapter; production multi-user deployment still requires PostgreSQL, hardened identity/token rotation, TLS, rate limiting, backups and operational monitoring.


## 5. Git integration controls

Promotion routes are disabled unless the API process is constructed with a trusted `GitWorktreeManager`. This is intentional: API callers must never choose a repository root, worktree directory, Git executable, or target branch. The standard app factory can construct the manager from the operator-controlled `BCS_GIT_REPOSITORY_ROOT` and `BCS_GIT_WORKTREE_ROOT` environment variables (both must be set). Alternatively, the hosting application may construct and inject it into `create_app(runtime=..., authenticator=..., git_manager=...)`. Keep the source repository checkout and worktree storage on persistent, access-restricted volumes; the manager requires the worktree root to be outside the repository. The manager's base branch is fixed to `development`.

A human requests promotion through the promotion-request route. The API creates a fingerprint-bound `release.deploy` approval request; an authorized human then decides it through the approvals decision route. Only after that approval may a human explicitly call the execute route. The executor rechecks QA, commit identity, branch ancestry and worktree cleanliness before a local fast-forward. It does not push to GitHub or deploy an application. `main` is never a target.

The current API setup is a development integration contract, not a complete cloud deployment recipe. Production still needs a hosting entrypoint that constructs and injects the trusted manager, durable shared storage (PostgreSQL is the planned production adapter), persistent Git volumes, TLS, token rotation, rate limiting, backups and monitoring.


### Model provider and worker startup

Configure at least one model endpoint before launching the API. For local Ollama's OpenAI-compatible endpoint, set `BCS_OLLAMA_MODEL` and optionally `BCS_OLLAMA_BASE_URL` (default `http://127.0.0.1:11434/v1`). For a cloud OpenAI-compatible endpoint, set `BCS_CLOUD_MODEL`, `BCS_CLOUD_BASE_URL`, and optionally `BCS_CLOUD_API_KEY_ENV` (default `OPENAI_API_KEY`); put the actual API key only in the named environment variable or secret manager. Set `BCS_DEFAULT_PROVIDER` to `ollama` or `cloud` when both are configured. No provider credentials are stored in source control.

Set `BCS_GIT_REPOSITORY_ROOT` to the trusted source checkout and `BCS_GIT_WORKTREE_ROOT` to a persistent directory outside that checkout. The source checkout must already contain the local `development` branch. The manager provisions a separate worktree per task workspace group. The app factory registers nine specialist roles when both the manager and a provider are available. The default worker mode is bounded autonomous file editing; set `BCS_WORKER_MODE=supervised` to require approvals for writes. Model workers cannot execute shell commands, run tests, deploy, or approve releases. QA/test execution remains a separate sandboxed workflow.

The development API uses a durable SQLite-backed queue and a process-local runner. The job record survives browser disconnects and process restarts; queued jobs and expired leases can be reclaimed. Execution is at-least-once, not exactly-once: a crash after a worker changes files but before job completion is committed can cause a reclaimed job to revisit its task IDs. The orchestrator fails in-flight task records closed on restart, so inspect task/workspace state before retrying uncertain work. SQLite is suitable for this single-instance development adapter, not a multi-replica cloud queue; production needs shared PostgreSQL/Redis-backed leasing, distributed worker coordination, monitoring and explicit recovery tooling.


## 6. Executable integration QA profiles

The operator may configure named, immutable test profiles through `BCS_QA_PROFILES_JSON`. Clients submit only a profile ID to `POST /v1/integrations/{integration_id}/qa`; they cannot supply command arguments, Docker images, repository paths or resource limits. Every profile command's executable must appear in the comma-separated `BCS_QA_ALLOWED_EXECUTABLES` allowlist. Configure `BCS_QA_DOCKER_IMAGE` and include that exact image in `BCS_QA_ALLOWED_IMAGES`. `BCS_DOCKER_EXECUTABLE` defaults to `docker`; `BCS_QA_MODE` defaults to `BCS_WORKER_MODE` (or `autonomous`) and accepts `supervised` or `autonomous`.

Example operator configuration:

```bash
BCS_QA_PROFILES_JSON='{"python-unit":[{"check_id":"unit","name":"Python unit tests","argv":["python","-m","unittest","discover","-s","tests"]}]}'
BCS_QA_ALLOWED_EXECUTABLES='python,pytest'
BCS_QA_DOCKER_IMAGE='bcs-qa:stable'
BCS_QA_ALLOWED_IMAGES='bcs-qa:stable'
BCS_QA_MODE='autonomous'
```

The route requires a human principal with `qa:execute`, checks the integration checkout against its recorded commit, and invokes the existing network-disabled Docker sandbox. The API call is synchronous in this development implementation; sandbox time and output limits still apply, but a long-running request can outlive a client connection. A later queue integration should make QA dispatch itself a durable background job. A successful result is recorded as `passed` only when at least one command actually executed and every check passed; static-only assessments remain `not_assessed`. No profile is enabled by default, and no test command is inferred from untrusted project files.

