# BCS API setup (development)

The control API is a FastAPI application. It exposes health, top-level task intake, task status, approval decisions and QA feedback history.

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
      "approvals:decide"
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
| GET | `/v1/feedback/{task_id}` | `tasks:read` |

The API currently accepts and records top-level tasks but does not yet dispatch them to live model-backed workers. That integration is a later implementation step. SQLite is a development persistence adapter; production multi-user deployment still requires PostgreSQL, hardened identity/token rotation, TLS, rate limiting, backups and operational monitoring.
