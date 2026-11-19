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
| POST | `/v1/tasks` | Human principal with `tasks:create` |
| GET | `/v1/tasks` | `tasks:read` |
| GET | `/v1/approvals` | `approvals:read` |
| POST | `/v1/approvals/{request_id}/decision` | Human principal with `approvals:decide` |
| GET | `/v1/feedback/{task_id}` | `tasks:read` |\n| GET | `/v1/integrations` | `integrations:read` |\n| GET | `/v1/integrations/{integration_id}/qa` | `integrations:read` |\n| GET | `/v1/integration-promotions` | `integrations:read` |\n| POST | `/v1/integrations/{integration_id}/promotion-requests` | Human principal with `integrations:promote` |\n| POST | `/v1/integration-promotions/{request_id}/execute` | Human principal with `integrations:promote`, after approval |

The API currently accepts and records top-level tasks but does not yet dispatch them to live model-backed workers. That integration is a later implementation step. SQLite is a development persistence adapter; production multi-user deployment still requires PostgreSQL, hardened identity/token rotation, TLS, rate limiting, backups and operational monitoring.


## 5. Git integration controls

Promotion routes are disabled unless the API process is constructed with a trusted `GitWorktreeManager`. This is intentional: API callers must never choose a repository root, worktree directory, Git executable, or target branch. The manager is created by the hosting application from operator-controlled configuration and injected into `create_app(runtime=..., authenticator=..., git_manager=...)`. Keep the source repository checkout and worktree storage on persistent, access-restricted volumes; the manager requires the worktree root to be outside the repository. The manager's base branch is fixed to `development`.

A human requests promotion through the promotion-request route. The API creates a fingerprint-bound `release.deploy` approval request; an authorized human then decides it through the approvals decision route. Only after that approval may a human explicitly call the execute route. The executor rechecks QA, commit identity, branch ancestry and worktree cleanliness before a local fast-forward. It does not push to GitHub or deploy an application. `main` is never a target.

The current API setup is a development integration contract, not a complete cloud deployment recipe. Production still needs a hosting entrypoint that constructs and injects the trusted manager, durable shared storage (PostgreSQL is the planned production adapter), persistent Git volumes, TLS, token rotation, rate limiting, backups and monitoring.
