"""HTTP API tests: authentication, scopes and approval identity boundaries."""
import hashlib
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.api import create_app, _git_manager_from_environment
from bcs.approvals import ActorType
from bcs.auth import Principal, TokenAuthenticator
from bcs.policy import ToolRequest, OperatingMode
from bcs.providers import ProviderResponse
from bcs.workspace import Workspace
from bcs.runtime import create_runtime


class BCSAPITests(unittest.TestCase):
    def setUp(self):
        self.runtime = create_runtime(database_path=":memory:")
        self.human_token = "human-test-secret"
        self.worker_token = "worker-test-secret"
        self.human = Principal(
            "owner-configured", ActorType.HUMAN,
            frozenset({"tasks:read", "tasks:create", "approvals:read", "approvals:decide", "integrations:read", "integrations:promote", "tasks:execute"}),
        )
        self.worker = Principal(
            "worker-configured", ActorType.WORKER,
            frozenset({"tasks:read", "approvals:read", "approvals:decide", "integrations:read", "integrations:promote"}),
        )
        auth = TokenAuthenticator({
            hashlib.sha256(self.human_token.encode()).hexdigest(): self.human,
            hashlib.sha256(self.worker_token.encode()).hexdigest(): self.worker,
        })
        self.client = TestClient(create_app(runtime=self.runtime, authenticator=auth))

    def tearDown(self):
        self.client.close()
        self.runtime.close()

    def test_health_is_available_without_credentials(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_human_can_create_task_but_worker_cannot(self):
        created = self.client.post(
            "/v1/tasks",
            headers={"Authorization": f"Bearer {self.human_token}"},
            json={
                "objective": "Build the requested app and preserve its behavior.",
                "acceptance_criteria": ["Registration works", "QA verifies the flow"],
            },
        )
        self.assertEqual(created.status_code, 201)
        task = created.json()["task"]
        self.assertEqual(task["state"], "queued")
        self.assertIn("preserve its behavior", task["task"]["objective"])

        denied = self.client.post(
            "/v1/tasks",
            headers={"Authorization": f"Bearer {self.worker_token}"},
            json={"objective": "Worker attempts to create an owner task."},
        )
        self.assertEqual(denied.status_code, 403)

    def test_protected_endpoint_requires_bearer_token(self):
        response = self.client.get("/v1/tasks")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Bearer")

    def test_protected_endpoint_enforces_scopes(self):
        response = self.client.get(
            "/v1/tasks", headers={"Authorization": f"Bearer {self.worker_token}"}
        )
        self.assertEqual(response.status_code, 200)
        denied = self.client.get(
            "/v1/approvals",
            headers={"Authorization": f"Bearer {self.worker_token}"},
        )
        self.assertEqual(denied.status_code, 200)

        no_scope = Principal("read-only", ActorType.HUMAN, frozenset({"tasks:read"}))
        digest = hashlib.sha256(b"read-only-token").hexdigest()
        self.client.app.state.bcs_authenticator = TokenAuthenticator({digest: no_scope})
        denied = self.client.get(
            "/v1/approvals", headers={"Authorization": "Bearer read-only-token"}
        )
        self.assertEqual(denied.status_code, 403)

    def test_only_configured_human_identity_can_decide_approval(self):
        request = ToolRequest(
            request_id="approval-1", task_id="task-1", worker_id="worker-1",
            workspace_id="workspace-1", capability="release.deploy",
            arguments={"target": "staging"},
        )
        self.runtime.approvals.submit(request)

        worker_response = self.client.post(
            "/v1/approvals/approval-1/decision",
            headers={"Authorization": f"Bearer {self.worker_token}"},
            json={"approved": True, "reason": "worker claims owner approved"},
        )
        self.assertEqual(worker_response.status_code, 403)
        self.assertEqual(self.runtime.approvals.get("approval-1").status.value, "pending")

        human_response = self.client.post(
            "/v1/approvals/approval-1/decision",
            headers={"Authorization": f"Bearer {self.human_token}"},
            json={"approved": True, "reason": "Owner review"},
        )
        self.assertEqual(human_response.status_code, 200)
        self.assertEqual(human_response.json()["approval"]["decided_by"], "owner-configured")

    def test_integration_history_requires_authentication_and_scope(self):
        missing = self.client.get("/v1/integrations")
        self.assertEqual(missing.status_code, 401)
        allowed = self.client.get(
            "/v1/integrations", headers={"Authorization": f"Bearer {self.human_token}"}
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json(), {"integrations": []})

    def test_promotion_api_requires_human_and_trusted_server_git_manager(self):
        worker = self.client.post(
            "/v1/integrations/example/promotion-requests",
            headers={"Authorization": f"Bearer {self.worker_token}"},
        )
        self.assertEqual(worker.status_code, 403)
        human_without_manager = self.client.post(
            "/v1/integrations/example/promotion-requests",
            headers={"Authorization": f"Bearer {self.human_token}"},
        )
        self.assertEqual(human_without_manager.status_code, 503)

    def test_human_can_dispatch_model_worker_and_worker_cannot_start_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.runtime.providers.complete = lambda messages, **kwargs: ProviderResponse(
                "test", "test-model",
                '{"summary":"Created module","edits":[{"path":"module.py","content":"VALUE = 7\n"}],"blockers":[]}',
            )
            self.runtime.register_model_worker(
                worker_id="builder-api-test",
                role="builder",
                workspace_factory=lambda task: Workspace("workspace-api-test", root),
                mode=OperatingMode.AUTONOMOUS,
            )
            created = self.client.post(
                "/v1/tasks",
                headers={"Authorization": f"Bearer {self.human_token}"},
                json={"objective": "Create a module exposing VALUE."},
            )
            task_id = created.json()["task"]["task"]["task_id"]
            denied = self.client.post(
                "/v1/tasks/dispatch",
                headers={"Authorization": f"Bearer {self.worker_token}"},
                json={"task_ids": [task_id]},
            )
            self.assertEqual(denied.status_code, 403)
            dispatched = self.client.post(
                "/v1/tasks/dispatch",
                headers={"Authorization": f"Bearer {self.human_token}"},
                json={"task_ids": [task_id]},
            )
            self.assertEqual(dispatched.status_code, 200)
            self.assertEqual(dispatched.json()["outcomes"][0]["state"], "completed")
            self.assertEqual((root / "module.py").read_text(), "VALUE = 7\\n")

    def test_git_manager_requires_both_operator_configured_paths(self):
        with patch.dict(
            "os.environ",
            {"BCS_GIT_REPOSITORY_ROOT": "/tmp/repo"},
            clear=True,
        ):
            with self.assertRaises(RuntimeError):
                _git_manager_from_environment()
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(_git_manager_from_environment())

    def test_decision_body_rejects_unrecognized_fields(self):
        response = self.client.post(
            "/v1/approvals/unknown/decision",
            headers={"Authorization": f"Bearer {self.human_token}"},
            json={"approved": True, "actor_type": "human"},
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
