"""HTTP API tests: authentication, scopes and approval identity boundaries."""
import hashlib
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.api import create_app, _git_manager_from_environment, _qa_profiles_from_environment
from bcs.approvals import ActorType
from bcs.orchestrator import WorkerResult, WorkerResultStatus
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
            frozenset({"tasks:read", "tasks:create", "approvals:read", "approvals:decide", "integrations:read", "integrations:promote", "tasks:execute", "qa:execute"}),
        )
        self.worker = Principal(
            "worker-configured", ActorType.WORKER,
            frozenset({"tasks:read", "approvals:read", "approvals:decide", "integrations:read", "integrations:promote", "tasks:execute", "qa:execute"}),
        )
        auth = TokenAuthenticator({
            hashlib.sha256(self.human_token.encode()).hexdigest(): self.human,
            hashlib.sha256(self.worker_token.encode()).hexdigest(): self.worker,
        })
        self.client = TestClient(create_app(runtime=self.runtime, authenticator=auth))

    def tearDown(self):
        self.client.app.state.bcs_execution_jobs.stop(timeout=2)
        self.client.close()
        self.runtime.close()

    def test_dashboard_is_served_by_the_control_api(self):
        home = self.client.get("/", follow_redirects=False)
        self.assertEqual(home.status_code, 307)
        self.assertEqual(home.headers["location"], "/dashboard/")
        page = self.client.get("/dashboard/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("BCS", page.text)
        script = self.client.get("/dashboard/app.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("/v1/builds", script.text)

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
                '{"summary":"Created module","edits":[{"path":"module.py","content":"VALUE = 7\\n"}],"blockers":[]}',
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
            self.assertEqual(dispatched.status_code, 202)
            job_id = dispatched.json()["job"]["job_id"]
            self.assertEqual(dispatched.json()["job"]["state"], "queued")
            deadline = time.monotonic() + 5
            status_response = self.client.get(
                f"/v1/execution-jobs/{job_id}",
                headers={"Authorization": f"Bearer {self.human_token}"},
            )
            while status_response.status_code == 200 and status_response.json()["job"]["state"] in {"queued", "running"} and time.monotonic() < deadline:
                time.sleep(0.05)
                status_response = self.client.get(
                    f"/v1/execution-jobs/{job_id}",
                    headers={"Authorization": f"Bearer {self.human_token}"},
                )
            self.assertEqual(status_response.status_code, 200)
            self.assertEqual(status_response.json()["job"]["state"], "completed")
            self.assertEqual((root / "module.py").read_text(), "VALUE = 7\n")
            other_owner = Principal("other-human", ActorType.HUMAN, frozenset({"tasks:read"}))
            digest = hashlib.sha256(b"other-owner-token").hexdigest()
            self.client.app.state.bcs_authenticator = TokenAuthenticator({digest: other_owner})
            hidden = self.client.get(
                f"/v1/execution-jobs/{job_id}",
                headers={"Authorization": "Bearer other-owner-token"},
            )
            self.assertEqual(hidden.status_code, 404)


    def test_human_can_submit_one_request_brain_build_job(self):
        class FakePlannerProvider:
            def complete(self, messages, **kwargs):
                return ProviderResponse(
                    "fake", "fake-model",
                    '{"steps":[{"step_id":"build","role":"builder",'
                    '"objective":"Build the requested module.",'
                    '"acceptance_criteria":["The module is created."],'
                    '"depends_on":[]}]}',
                )

        self.runtime.planner.providers = FakePlannerProvider()
        self.runtime.orchestrator.register_worker(
            worker_id="one-request-builder", role="builder",
            handler=lambda task: WorkerResult(
                WorkerResultStatus.COMPLETED, "Built the requested module.",
                changed_files=("module.py",),
            ),
        )
        response = self.client.post(
            "/v1/builds",
            headers={"Authorization": f"Bearer {self.human_token}"},
            json={"objective": "Build a simple module.", "acceptance_criteria": ["The module is created."]},
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(response.json()["task_ids"]), 1)
        job_id = response.json()["job"]["job_id"]
        deadline = time.monotonic() + 5
        status_response = self.client.get(
            f"/v1/execution-jobs/{job_id}",
            headers={"Authorization": f"Bearer {self.human_token}"},
        )
        while status_response.status_code == 200 and status_response.json()["job"]["state"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.05)
            status_response = self.client.get(
                f"/v1/execution-jobs/{job_id}",
                headers={"Authorization": f"Bearer {self.human_token}"},
            )
        self.assertEqual(status_response.json()["job"]["state"], "completed")


    def test_human_can_enqueue_server_profiled_integration_qa(self):
        profile_json = (
            '{"python-unit":[{"check_id":"unit","name":"Python unit tests",'
            '"argv":["python","-m","unittest","discover","-s","tests"]}]}'
        )
        env = {
            "BCS_QA_PROFILES_JSON": profile_json,
            "BCS_QA_ALLOWED_EXECUTABLES": "python,pytest",
            "BCS_QA_DOCKER_IMAGE": "bcs-qa:stable",
            "BCS_QA_ALLOWED_IMAGES": "bcs-qa:stable",
        }
        class FakeJobs:
            def __init__(self):
                self.submitted = None
            def start(self):
                pass
            def stop(self, timeout=5):
                return True
            def enqueue_workflow(self, job_type, task_ids, requested_by, *, metadata=None):
                self.submitted = (job_type, tuple(task_ids), requested_by, metadata)
                return {
                    "job_id": "job-qa-test", "state": "queued",
                    "job_type": job_type, "metadata": metadata,
                }
        jobs = FakeJobs()
        with patch.dict("os.environ", env, clear=False):
            app = create_app(
                runtime=self.runtime, authenticator=self.client.app.state.bcs_authenticator,
                git_manager=object(), execution_job_service=jobs,
            )
        integration = {
            "integration_id": "integration-a", "status": "awaiting_qa",
            "commit_sha": "a" * 40,
        }
        with patch.object(self.runtime, "integration_history", return_value=[integration]):
            with TestClient(app) as client:
                response = client.post(
                    "/v1/integrations/integration-a/qa",
                    headers={"Authorization": f"Bearer {self.human_token}"},
                    json={"profile_id": "python-unit"},
                )
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.json()["job"]["state"], "queued")
                self.assertEqual(response.json()["status_url"], "/v1/execution-jobs/job-qa-test")
        self.assertEqual(jobs.submitted[0], "integration_qa")
        self.assertEqual(jobs.submitted[1], ("integration-a",))
        self.assertEqual(jobs.submitted[3]["profile_id"], "python-unit")
        self.assertEqual(jobs.submitted[3]["commit_sha"], "a" * 40)

    def test_worker_cannot_start_integration_qa_even_with_scope(self):
        with patch.dict("os.environ", {
            "BCS_QA_PROFILES_JSON": '{"tests":[{"check_id":"unit","name":"Unit","argv":["pytest"]}]}',
            "BCS_QA_ALLOWED_EXECUTABLES": "pytest",
            "BCS_QA_DOCKER_IMAGE": "bcs-qa:stable",
            "BCS_QA_ALLOWED_IMAGES": "bcs-qa:stable",
        }, clear=False):
            app = create_app(
                runtime=self.runtime, authenticator=self.client.app.state.bcs_authenticator,
                git_manager=object(),
            )
        client = TestClient(app)
        response = client.post(
            "/v1/integrations/integration-a/qa",
            headers={"Authorization": f"Bearer {self.worker_token}"},
            json={"profile_id": "tests"},
        )
        self.assertEqual(response.status_code, 403)
        client.close()

    def test_qa_profile_rejects_unapproved_executable(self):
        with patch.dict("os.environ", {
            "BCS_QA_PROFILES_JSON": '{"unsafe":[{"check_id":"one","name":"Unsafe","argv":["sh","-c","id"]}]}',
            "BCS_QA_ALLOWED_EXECUTABLES": "python,pytest",
        }, clear=False):
            with self.assertRaises(RuntimeError):
                _qa_profiles_from_environment()

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
