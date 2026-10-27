import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.helpers import tool_message
from tests.test_verification_runtime import explanation, proposal, runtime_fixture
from tests.test_web import headers, web_module
from tests.verification_helpers import verification_module

API = "/api/conversations"


def wait_conversation(client, conversation_id, auth):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(f"{API}/{conversation_id}", headers=auth)
        assert response.status_code == 200, response.text
        record = response.json()
        if record["status"] not in ("planning", "running"):
            return record
        time.sleep(0.02)
    pytest.fail("Verification conversation did not finish its turn")


def submit(client, auth, root, message="Verify pitch step response."):
    response = client.post(API, headers=auth, json={"project_root": str(root), "message": message})
    assert response.status_code == 202, response.text
    return wait_conversation(client, response.json()["id"], auth)


def confirm(client, auth, conversation):
    response = client.post(
        f"{API}/{conversation['id']}/confirm",
        headers=auth,
        json={"version": conversation["task"]["baseline"]["version"], "trusted": True},
    )
    assert response.status_code == 202, response.text
    return wait_conversation(client, conversation["id"], auth)


def artifact_url(conversation, artifact, task=None):
    task = task or conversation["task"]
    return f"{API}/{conversation['id']}/tasks/{task['id']}/artifacts/{artifact['path']}"


def test_directory_agent_is_default_and_legacy_workbench_is_preserved(project, tmp_path):
    runtime, _, _, _ = runtime_fixture(tmp_path)
    with TestClient(
        web_module().create_web_app(project, verification_runtime=runtime),
        base_url="http://127.0.0.1",
    ) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "飞控工程 Agent" in response.text
        assert 'id="resource-location"' in response.text
        assert 'id="scenario"' not in response.text
        assert "MATLAB 分析工作台" in client.get("/workbench").text
        for name in ("agent.js", "agent.css", "app.js", "style.css"):
            assert client.get(f"/static/{name}").status_code == 200
        assert client.get("/static/../config.py").status_code == 404
        assert "script-src 'self'" in response.headers["content-security-policy"]


def test_recovery_plan_endpoint_requires_consent_and_does_not_execute(
    project, tmp_path, monkeypatch
):
    from tests.test_verification_replanning import failed_delivery

    runtime, backend, _, _, failed, _ = failed_delivery(
        tmp_path, monkeypatch, proposal(threshold=4)
    )
    with TestClient(
        web_module().create_web_app(project, verification_runtime=runtime),
        base_url="http://127.0.0.1",
    ) as client:
        endpoint = f"{API}/{failed.id}/replan"
        assert client.post(endpoint, json={"version": 1, "trusted": True}).status_code == 403
        auth = headers(client)
        assert client.post(
            endpoint, headers=auth, json={"version": 1, "trusted": False}
        ).status_code == 422
        assert client.post(
            endpoint, headers=auth, json={"version": 2, "trusted": True}
        ).status_code == 409
        response = client.post(endpoint, headers=auth, json={"version": 1, "trusted": True})
        assert response.status_code == 202, response.text
        pending = wait_conversation(client, failed.id, auth)
        assert pending["pending_decision"]["kind"] == "confirm_baseline"
        assert pending["task"]["baseline"]["version"] == 2
        assert pending["task"]["baseline"]["confirmed"] is False
        assert len(backend.calls) == 1


def test_conversation_endpoints_require_local_authentication(project, tmp_path):
    runtime, backend, _, root = runtime_fixture(tmp_path)
    with TestClient(
        web_module().create_web_app(project, verification_runtime=runtime),
        base_url="http://127.0.0.1",
    ) as client:
        assert client.get(API).status_code == 403
        assert (
            client.post(API, json={"message": "Verify", "project_root": str(root)}).status_code
            == 403
        )
        auth = headers(client)
        assert client.get(API, headers=auth).json() == []
        assert client.get(f"{API}/{'a' * 32}", headers=auth).status_code == 404
        response = client.post(
            API,
            headers={**auth, "Origin": "https://untrusted.example"},
            json={"message": "Verify", "project_root": str(root)},
        )
        assert response.status_code == 403
        assert not backend.calls


@pytest.mark.parametrize(
    "body",
    [
        {"message": ""},
        {"message": "   "},
        {"message": "x" * 4001},
        {"message": "Verify", "execute_matlab": "unregistered()"},
        {"message": "Verify", "project_root": 123},
        {"message": "Verify", "output_root": 123},
    ],
)
def test_invalid_messages_do_not_create_persistent_conversations(project, tmp_path, body):
    runtime, _, _, _ = runtime_fixture(tmp_path)
    with TestClient(
        web_module().create_web_app(project, verification_runtime=runtime),
        base_url="http://127.0.0.1",
    ) as client:
        auth = headers(client)
        assert client.post(API, headers=auth, json=body).status_code == 422
        assert runtime.store.recent() == []


def test_api_confirmation_evidence_reuse_and_integrity_checked_downloads(project, tmp_path):
    runtime, backend, _, root = runtime_fixture(tmp_path, [proposal(), proposal(threshold=4)])
    with TestClient(
        web_module().create_web_app(project, verification_runtime=runtime),
        base_url="http://127.0.0.1",
    ) as client:
        auth = headers(client)
        planned = submit(client, auth, root)
        assert planned["pending_decision"]["kind"] == "confirm_baseline"
        assert planned["task"]["baseline"]["confirmed"] is False
        assert not backend.calls
        endpoint = f"{API}/{planned['id']}/confirm"
        for trusted in (False, "true", 1):
            response = client.post(endpoint, headers=auth, json={"version": 1, "trusted": trusted})
            assert response.status_code == 422, response.text
        stale = client.post(endpoint, headers=auth, json={"version": 9, "trusted": True})
        assert stale.status_code == 409, stale.text
        assert not backend.calls
        done = confirm(client, auth, planned)
        assert done["status"] == "completed", done
        assert done["task"]["evaluation"]["status"] == "pass"
        artifacts = done["task"]["artifacts"]
        assert {Path(item["path"]).suffix for item in artifacts} >= {
            ".docx",
            ".png",
            ".mat",
            ".json",
        }
        for item in artifacts:
            url = artifact_url(done, item)
            assert client.get(url).status_code == 403
            response = client.get(url, headers=auth)
            assert response.status_code == 200, response.text
            assert len(response.content) == item["size"]
            assert response.headers["content-type"].startswith(item["media_type"])
            assert "attachment" in response.headers["content-disposition"]
        changed = client.post(
            f"{API}/{done['id']}/messages",
            headers=auth,
            json={"message": "Change only the overshoot threshold to 4%."},
        )
        assert changed.status_code == 202, changed.text
        revised = wait_conversation(client, done["id"], auth)
        assert revised["task"]["baseline"]["version"] == 2
        done = confirm(client, auth, revised)
        assert done["task"]["evaluation"]["status"] == "fail"
        assert len(backend.calls) == 1
        recent = client.get(API, headers=auth).json()
        assert recent[0]["id"] == done["id"]
        report = next(item for item in artifacts if item["role"] == "report")
        report_path = Path(done["task"]["result_directory"]) / report["path"]
        report_path.write_bytes(b"changed report")
        assert client.get(artifact_url(done, report), headers=auth).status_code == 409
        secret = Path(done["task"]["result_directory"]) / "unlisted.log"
        secret.write_text("not a published artifact")
        assert (
            client.get(artifact_url(done, {"path": secret.name}), headers=auth).status_code == 404
        )
        wrong_task = {**done["task"], "id": "a" * 32}
        assert client.get(artifact_url(done, report, wrong_task), headers=auth).status_code == 404


def test_external_output_api_preserves_authorized_current_and_archived_downloads_after_restart(
    project, tmp_path
):
    runtime, backend, _, root = runtime_fixture(tmp_path, [proposal(), proposal(threshold=4)])
    output = tmp_path / "external results"
    with TestClient(
        web_module().create_web_app(project, verification_runtime=runtime),
        base_url="http://127.0.0.1",
    ) as client:
        auth = headers(client)
        response = client.post(
            API,
            headers=auth,
            json={
                "message": "Verify pitch.",
                "project_root": str(root),
                "output_root": str(output),
            },
        )
        assert response.status_code == 202, response.text
        planned = wait_conversation(client, response.json()["id"], auth)
        directory = Path(planned["task"]["result_directory"])
        assert directory.parent == output
        assert str(directory) in planned["pending_decision"]["message"]
        assert not backend.calls
        first = confirm(client, auth, planned)
        assert first["status"] == "completed", first
        original = {
            item["path"]: (directory / item["path"]).read_bytes()
            for item in first["task"]["artifacts"]
        }
        changed_output = tmp_path / "new results"
        response = client.post(
            f"{API}/{first['id']}/messages",
            headers=auth,
            json={
                "message": "Change output and threshold to 4%.",
                "output_root": str(changed_output),
            },
        )
        assert response.status_code == 202, response.text
        revised = wait_conversation(client, first["id"], auth)
        assert revised["task"]["id"] != first["task"]["id"]
        assert revised["task"]["baseline"]["confirmed"] is False
        assert Path(revised["task"]["result_directory"]).parent == changed_output
        final = confirm(client, auth, revised)
        assert final["status"] == "completed", final
        assert final["task"]["evaluation"]["status"] == "fail"
        assert len(backend.calls) == 1
        assert all((directory / name).read_bytes() == data for name, data in original.items())

    restored = verification_module("runtime").VerificationRuntime(
        tmp_path / "state", backend=backend, planner=runtime.planner
    )
    with TestClient(
        web_module().create_web_app(project, verification_runtime=restored),
        base_url="http://127.0.0.1",
    ) as client:
        auth = headers(client)
        final = client.get(f"{API}/{first['id']}", headers=auth).json()
        assert final["output_root"] == str(changed_output)
        assert final["archived_tasks"][0]["id"] == first["task"]["id"]
        for task in [final["task"], *final["archived_tasks"]]:
            directory = Path(task["result_directory"])
            for item in task["artifacts"]:
                url = artifact_url(final, item, task)
                assert client.get(url).status_code == 403
                response = client.get(url, headers=auth)
                assert response.status_code == 200, response.text
                assert response.content == (directory / item["path"]).read_bytes()
            unlisted = directory / "unlisted.json"
            unlisted.write_text('{"private": true}')
            assert client.get(
                artifact_url(final, {"path": unlisted.name}, task), headers=auth
            ).status_code == 404
            assert client.get(
                artifact_url(final, {"path": "%2e%2e%2funlisted.json"}, task), headers=auth
            ).status_code == 404
            report = next(item for item in task["artifacts"] if item["role"] == "report")
            (directory / report["path"]).write_bytes(b"tampered report")
            assert client.get(
                artifact_url(final, report, task), headers=auth
            ).status_code == 409
        assert len(backend.calls) == 1


@pytest.mark.parametrize("replacement", ["artifact", "task", "root"])
def test_external_output_download_rejects_symlink_replacements(project, tmp_path, replacement):
    runtime, _, _, root = runtime_fixture(tmp_path)
    output = tmp_path / "external"
    planned = runtime.submit(None, "Verify pitch.", str(root), output_root=str(output))
    done = runtime.confirm(planned.id, 1, trusted=True).model_dump()
    task = done["task"]
    artifact = next(item for item in task["artifacts"] if item["role"] == "report")
    directory = Path(task["result_directory"])
    replaced = {
        "artifact": directory / artifact["path"],
        "task": directory,
        "root": output,
    }[replacement]
    relocated = tmp_path / ("relocated.docx" if replacement == "artifact" else "relocated")
    replaced.rename(relocated)
    replaced.symlink_to(relocated, target_is_directory=replacement != "artifact")
    with TestClient(
        web_module().create_web_app(project, verification_runtime=runtime),
        base_url="http://127.0.0.1",
    ) as client:
        response = client.get(artifact_url(done, artifact), headers=headers(client))
        assert response.status_code == 409, response.text
        assert "symlink" in response.json()["detail"].lower()


def test_planning_and_execution_do_not_block_status_and_busy_posts_are_rejected(project, tmp_path):
    runtime, backend, _, root = runtime_fixture(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    inspect = backend.inspect

    def slow_inspect(directory):
        entered.set()
        assert release.wait(5)
        return inspect(directory)

    backend.inspect = slow_inspect
    with (
        TestClient(
            web_module().create_web_app(project, verification_runtime=runtime),
            base_url="http://127.0.0.1",
        ) as client,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        auth = headers(client)
        try:
            posted = pool.submit(
                client.post,
                API,
                headers=auth,
                json={"message": "Verify pitch.", "project_root": str(root)},
            ).result(timeout=2)
            assert posted.status_code == 202, posted.text
            assert entered.wait(2)
            identifier = posted.json()["id"]
            response = pool.submit(client.get, f"{API}/{identifier}", headers=auth).result(
                timeout=1
            )
            saved = response.json()
            assert saved["status"] == "planning"
            assert saved["messages"][-1]["content"] == "Verify pitch."
            assert runtime.store.get(identifier).project_root == str(root)
            busy = client.post(
                API, headers=auth, json={"message": "Another request", "project_root": str(root)}
            )
            assert busy.status_code == 409
            assert len(runtime.store.recent()) == 1
        finally:
            release.set()
        planned = wait_conversation(client, identifier, auth)
        entered.clear()
        release.clear()
        execute = backend.execute
        backend.inspect = inspect

        def slow_execute(request, directory, on_progress, *, resume=False):
            entered.set()
            assert release.wait(5)
            return execute(request, directory, on_progress, resume=resume)

        backend.execute = slow_execute
        try:
            response = client.post(
                f"{API}/{identifier}/confirm",
                headers=auth,
                json={"version": 1, "trusted": True},
            )
            assert response.status_code == 202, response.text
            assert entered.wait(2)
            saved = pool.submit(client.get, f"{API}/{identifier}", headers=auth).result(timeout=1)
            assert saved.json()["status"] == "running"
            assert runtime.store.get(identifier).task.pending_request is not None
            busy = client.post(
                f"{API}/{identifier}/messages", headers=auth, json={"message": "Duplicate"}
            )
            assert busy.status_code == 409
        finally:
            release.set()
        done = wait_conversation(client, identifier, auth)
        assert done["status"] == "completed", done
        assert done["task"]["id"] == planned["task"]["id"]
        assert len(backend.calls) == 1


def test_api_resume_after_restart_does_not_resubmit_worker_execution(project, tmp_path):
    runtime, backend, model, root = runtime_fixture(tmp_path)
    backend.fail_after_completion = True
    model.responses = [
        tool_message("propose_verification", proposal()),
        tool_message(
            "propose_verification", {"action": "clarify", "question": "Resume the recorded run."}
        ),
        explanation(),
    ]
    with TestClient(
        web_module().create_web_app(project, verification_runtime=runtime),
        base_url="http://127.0.0.1",
    ) as client:
        auth = headers(client)
        stopped = confirm(client, auth, submit(client, auth, root))
        assert stopped["status"] == "interrupted", stopped
        assert stopped["task"]["pending_request"]
        retry = client.post(
            f"{API}/{stopped['id']}/retry",
            headers=auth,
            json={"version": 1, "trusted": True},
        )
        assert retry.status_code == 409, retry.text
    restored = verification_module("runtime").VerificationRuntime(
        tmp_path / "state", backend=backend, planner=runtime.planner
    )
    with TestClient(
        web_module().create_web_app(project, verification_runtime=restored),
        base_url="http://127.0.0.1",
    ) as client:
        auth = headers(client)
        response = client.post(f"{API}/{stopped['id']}/resume", headers=auth)
        assert response.status_code == 202, response.text
        done = wait_conversation(client, stopped["id"], auth)
        assert done["status"] == "completed", done
        assert [resume for _, resume in backend.calls] == [False, True]
        assert backend.calls[0][0].id == backend.calls[1][0].id


def test_background_failure_is_persisted_and_does_not_leave_the_api_busy(
    project, tmp_path, monkeypatch
):
    runtime, _, _, root = runtime_fixture(tmp_path)

    def unavailable(*args, **kwargs):
        raise OSError("Controlled storage failure")

    original = runtime.continue_submission
    monkeypatch.setattr(runtime, "continue_submission", unavailable)
    with TestClient(
        web_module().create_web_app(project, verification_runtime=runtime),
        base_url="http://127.0.0.1",
    ) as client:
        auth = headers(client)
        failed = submit(client, auth, root)
        assert failed["status"] == "failed"
        assert "Controlled storage failure" in failed["error"]
        assert failed["messages"][0]["content"] == "Verify pitch step response."
        monkeypatch.setattr(runtime, "continue_submission", original)
        planned = submit(client, auth, root)
        assert planned["pending_decision"]["kind"] == "confirm_baseline"


def test_scheduling_failure_is_reported_and_keeps_the_accepted_request(
    project, tmp_path, monkeypatch
):
    runtime, _, _, root = runtime_fixture(tmp_path)
    manager = verification_module("routes").ConversationManager
    original = manager._dispatch

    def unavailable(*args, **kwargs):
        raise RuntimeError("Controlled executor shutdown")

    monkeypatch.setattr(manager, "_dispatch", unavailable)
    with TestClient(
        web_module().create_web_app(project, verification_runtime=runtime),
        base_url="http://127.0.0.1",
        raise_server_exceptions=False,
    ) as client:
        auth = headers(client)
        response = client.post(
            API, headers=auth, json={"message": "Keep this request.", "project_root": str(root)}
        )
        assert response.status_code == 503, response.text
        saved = runtime.store.recent()[0]
        assert saved.status == "failed"
        assert "Controlled executor shutdown" in saved.error
        assert saved.messages[0].content == "Keep this request."
        monkeypatch.setattr(manager, "_dispatch", original)
        assert submit(client, auth, root)["pending_decision"]["kind"] == "confirm_baseline"


def test_failed_confirmation_scheduling_preserves_the_unconfirmed_baseline(
    project, tmp_path, monkeypatch
):
    runtime, backend, _, root = runtime_fixture(tmp_path)
    manager = verification_module("routes").ConversationManager
    original = manager._dispatch

    def unavailable(*args, **kwargs):
        raise RuntimeError("Controlled executor shutdown")

    with TestClient(
        web_module().create_web_app(project, verification_runtime=runtime),
        base_url="http://127.0.0.1",
        raise_server_exceptions=False,
    ) as client:
        auth = headers(client)
        planned = submit(client, auth, root)
        monkeypatch.setattr(manager, "_dispatch", unavailable)
        response = client.post(
            f"{API}/{planned['id']}/confirm",
            headers=auth,
            json={"version": 1, "trusted": True},
        )
        assert response.status_code == 503, response.text
        saved = runtime.store.get(planned["id"])
        assert saved.status == "failed"
        assert saved.pending_decision.kind == "confirm_baseline"
        assert saved.task.baseline.confirmed is False
        assert not backend.calls
        monkeypatch.setattr(manager, "_dispatch", original)
        assert confirm(client, auth, planned)["status"] == "completed"


@pytest.mark.parametrize("failure_mode", ["scheduling", "background"])
@pytest.mark.parametrize("origin", ["unconfirmed", "failed_delivery"])
def test_failed_followup_preserves_original_task_and_recovery(
    project, tmp_path, monkeypatch, failure_mode, origin
):
    if origin == "failed_delivery":
        from tests.test_verification_replanning import failed_delivery

        runtime, backend, model, _, before, deliver = failed_delivery(
            tmp_path, monkeypatch, {"action": "clarify", "question": "Repair the output?"}
        )
        monkeypatch.setattr(verification_module("runtime"), "deliver_report", deliver)
    else:
        runtime, backend, model, root = runtime_fixture(tmp_path)
        before = runtime.submit(None, "Verify pitch.", str(root))
    model_calls = len(model.seen)
    worker_calls = len(backend.calls)

    def unavailable(*args, **kwargs):
        raise RuntimeError("Controlled follow-up failure")

    with TestClient(
        web_module().create_web_app(project, verification_runtime=runtime),
        base_url="http://127.0.0.1",
    ) as client:
        auth = headers(client)
        with monkeypatch.context() as failures:
            if failure_mode == "scheduling":
                failures.setattr(
                    verification_module("routes").ConversationManager, "_dispatch", unavailable
                )
            else:
                failures.setattr(runtime, "continue_submission", unavailable)
            response = client.post(
                f"{API}/{before.id}/messages",
                headers=auth,
                json={"message": "Explain the current task."},
            )
            assert response.status_code == (503 if failure_mode == "scheduling" else 202)
            saved = wait_conversation(client, before.id, auth)
            assert saved["status"] == before.status
            assert saved["error"] == before.error
            assert saved["pending_decision"] == before.pending_decision.model_dump(mode="json")
            assert saved["task"] == before.task.model_dump(mode="json")
            assert saved["turn_origin"] is None
            assert saved["messages"][-1]["role"] == "assistant"
            assert "Controlled follow-up failure" in saved["messages"][-1]["content"]
            assert len(model.seen) == model_calls
            assert len(backend.calls) == worker_calls
            restarted = verification_module("store").ConversationStore(runtime.store.root)
            restored = restarted.get(before.id)
            assert restored.status == before.status
            assert restored.pending_decision == before.pending_decision
            assert restored.error == before.error

        if origin == "unconfirmed":
            completed = confirm(client, auth, saved)
        else:
            response = client.post(f"{API}/{before.id}/resume", headers=auth)
            assert response.status_code == 202, response.text
            completed = wait_conversation(client, before.id, auth)
        assert completed["status"] == "completed", completed["error"]
        assert len(backend.calls) == 1
