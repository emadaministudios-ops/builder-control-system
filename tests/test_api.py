"""HTTP API tests: authentication, scopes and approval identity boundaries."""
import hashlib
import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.api import create_app
from bcs.approvals import ActorType
from bcs.auth import Principal, TokenAuthenticator
from bcs.policy import ToolRequest
from bcs.runtime import create_runtime


class BCSAPITests(unittest.TestCase):
    def setUp(self):
        self.runtime = create_runtime(database_path=":memory:")
        self.human_token = "human-test-secret"
        self.worker_token = "worker-test-secret"
        self.human = Principal(
            "owner-configured", ActorType.HUMAN,
            frozenset({"tasks:read", "approvals:read", "approvals:decide"}),
        )
        self.worker = Principal(
            "worker-configured", ActorType.WORKER,
            frozenset({"tasks:read", "approvals:read", "approvals:decide"}),
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

    def test_decision_body_rejects_unrecognized_fields(self):
        response = self.client.post(
            "/v1/approvals/unknown/decision",
            headers={"Authorization": f"Bearer {self.human_token}"},
            json={"approved": True, "actor_type": "human"},
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
