import asyncio
import subprocess
from fastapi.testclient import TestClient
from ai_work_os.api import app
from ai_work_os import api as api_module
from ai_work_os import worker
from ai_work_os.db import SessionLocal
from ai_work_os.models import Task, TaskEvent, utcnow

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_dashboard_is_served():
    response = client.get("/")
    assert response.status_code == 200
    assert "AI Work OS" in response.text
    assert "function renderTaskResult(events)" in response.text
    assert "Agent result" in response.text


def test_stats_shape():
    response = client.get("/stats")
    assert response.status_code == 200
    payload = response.json()
    assert "total" in payload
    assert "by_status" in payload


def test_task_rejects_missing_workspace():
    response = client.post("/tasks", json={
        "title": "test",
        "prompt": "read only",
        "workspace_path": "/path/that/should/not/exist/ai-work-os-test",
    })
    assert response.status_code == 422


def test_create_task_and_read_events(tmp_path):
    response = client.post("/tasks", json={
        "title": "API smoke test",
        "prompt": "Read only",
        "workspace_path": str(tmp_path),
    })
    assert response.status_code == 201
    task = response.json()
    assert task["status"] == "queued"
    assert task["permission_mode"] == "plan"
    assert task["queue_wait_ms"] is not None
    assert task["execution_ms"] is None
    events = client.get(f"/tasks/{task['id']}/events")
    assert events.status_code == 200
    assert events.json()[0]["event_type"] == "task.queued"


def test_cancel_queued_task_closes_lifecycle(tmp_path):
    created = client.post("/tasks", json={
        "title": "Cancel before start",
        "prompt": "Do not execute",
        "workspace_path": str(tmp_path),
    })
    task_id = created.json()["id"]

    response = client.post(f"/tasks/{task_id}/cancel")
    assert response.status_code == 200
    task = response.json()
    assert task["status"] == "cancelled"
    assert task["finished_at"] is not None
    assert task["started_at"] is None
    assert task["execution_ms"] is None

    events = client.get(f"/tasks/{task_id}/events").json()
    assert [event["event_type"] for event in events][-1] == "task.cancelled"
    assert events[-1]["payload"]["cancelled_before_start"] is True


def test_missing_claude_executable_finishes_task_as_failed(tmp_path, monkeypatch):
    created = client.post("/tasks", json={
        "title": "Missing CLI test",
        "prompt": "This should fail before execution",
        "workspace_path": str(tmp_path),
    })
    task_id = created.json()["id"]
    monkeypatch.setattr(worker, "CLAUDE_BIN", str(tmp_path / "missing-claude"))
    monkeypatch.setattr(worker, "STORAGE", tmp_path / "storage")

    asyncio.run(worker.execute_task(task_id))

    task = client.get(f"/tasks/{task_id}").json()
    assert task["status"] == "failed"
    assert task["exit_code"] == 127
    assert "executable not found" in task["error_summary"]
    events = client.get(f"/tasks/{task_id}/events").json()
    assert events[-1]["event_type"] == "executor.finished"
    assert events[-1]["payload"]["status"] == "failed"


def test_permission_modes_are_persisted_and_applied():
    from ai_work_os.worker import build_command

    plan = client.post("/tasks", json={
        "title": "Default plan mode",
        "prompt": "Inspect without modifying",
        "workspace_path": ".",
    })
    assert plan.status_code == 201
    assert plan.json()["permission_mode"] == "plan"
    plan_command = build_command("inspect", "plan")
    assert plan_command[-2:] == ["--permission-mode", "plan"]

    configured = client.post("/tasks", json={
        "title": "Local configured mode",
        "prompt": "Use configured CLI permissions",
        "workspace_path": ".",
        "permission_mode": "configured",
    })
    assert configured.status_code == 201
    assert configured.json()["permission_mode"] == "configured"
    configured_command = build_command("inspect", "configured")
    assert "--permission-mode" not in configured_command


def test_event_timeline_compacts_thinking_tokens_but_preserves_raw_access(tmp_path):
    created = client.post("/tasks", json={
        "title": "Compact event timeline",
        "prompt": "Inspect events",
        "workspace_path": str(tmp_path),
    })
    task_id = created.json()["id"]
    with SessionLocal() as db:
        db.add_all([
            TaskEvent(task_id=task_id, event_type="claude.system", payload={
                "subtype": "thinking_tokens", "estimated_tokens": estimate,
            })
            for estimate in (3, 7, 12)
        ])
        db.commit()

    compact = client.get(
        f"/tasks/{task_id}/events?include_thinking_tokens=false"
    ).json()
    summaries = [
        event for event in compact
        if event["event_type"] == "claude.system.thinking_tokens_summary"
    ]
    assert len(summaries) == 1
    assert summaries[0]["payload"]["sample_count"] == 3
    assert summaries[0]["payload"]["latest_estimated_tokens"] == 12

    raw = client.get(
        f"/tasks/{task_id}/events?include_thinking_tokens=true"
    ).json()
    assert sum(
        event["payload"].get("subtype") == "thinking_tokens"
        for event in raw
    ) == 3



def test_worker_timeout_is_explicit_and_finishes_task(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text("#!/bin/sh\nexec sleep 10\n", encoding="utf-8")
    fake_claude.chmod(0o755)

    created = client.post("/tasks", json={
        "title": "Timeout test",
        "prompt": "Sleep for too long",
        "workspace_path": str(workspace),
    })
    task_id = created.json()["id"]
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        task.status = "running"
        task.started_at = utcnow()
        db.commit()

    monkeypatch.setattr(worker, "CLAUDE_BIN", str(fake_claude))
    monkeypatch.setattr(worker, "STORAGE", tmp_path / "storage")
    monkeypatch.setattr(worker, "MAX_TASK_SECONDS", 0.1)

    asyncio.run(worker.execute_task(task_id))

    task = client.get(f"/tasks/{task_id}").json()
    assert task["status"] == "failed"
    assert "timed out" in task["error_summary"]
    events = client.get(f"/tasks/{task_id}/events").json()
    timeout_events = [event for event in events if event["event_type"] == "executor.timed_out"]
    assert len(timeout_events) == 1
    assert timeout_events[0]["payload"]["timeout_seconds"] == 0.1
    assert events[-1]["event_type"] == "executor.finished"
    assert events[-1]["payload"]["terminal_reason"] == "timeout"



def test_running_task_cancellation_terminates_process_and_closes_lifecycle(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text("#!/bin/sh\nexec sleep 10\n", encoding="utf-8")
    fake_claude.chmod(0o755)

    created = client.post("/tasks", json={
        "title": "Running cancellation test",
        "prompt": "Sleep until cancelled",
        "workspace_path": str(workspace),
    })
    task_id = created.json()["id"]
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        task.status = "running"
        task.started_at = utcnow()
        db.commit()

    monkeypatch.setattr(worker, "CLAUDE_BIN", str(fake_claude))
    monkeypatch.setattr(worker, "STORAGE", tmp_path / "storage")
    monkeypatch.setattr(worker, "MAX_TASK_SECONDS", 30)

    async def execute_then_cancel():
        execution = asyncio.create_task(worker.execute_task(task_id))
        await asyncio.sleep(0.15)
        response = client.post(f"/tasks/{task_id}/cancel")
        assert response.status_code == 200
        await asyncio.wait_for(execution, timeout=5)

    asyncio.run(execute_then_cancel())

    task = client.get(f"/tasks/{task_id}").json()
    assert task["status"] == "cancelled"
    assert task["finished_at"] is not None
    events = client.get(f"/tasks/{task_id}/events").json()
    assert any(event["event_type"] == "executor.cancelled" for event in events)
    assert events[-1]["event_type"] == "executor.finished"
    assert events[-1]["payload"]["terminal_reason"] == "cancelled"



def test_worker_interruption_terminates_subprocess_and_records_failure(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text("#!/bin/sh\nexec sleep 10\n", encoding="utf-8")
    fake_claude.chmod(0o755)

    created = client.post("/tasks", json={
        "title": "Worker interruption test",
        "prompt": "Sleep until worker stops",
        "workspace_path": str(workspace),
    })
    task_id = created.json()["id"]
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        task.status = "running"
        task.started_at = utcnow()
        db.commit()

    monkeypatch.setattr(worker, "CLAUDE_BIN", str(fake_claude))
    monkeypatch.setattr(worker, "STORAGE", tmp_path / "storage")
    monkeypatch.setattr(worker, "MAX_TASK_SECONDS", 30)

    async def interrupt_worker():
        execution = asyncio.create_task(worker.execute_task(task_id))
        await asyncio.sleep(0.15)
        execution.cancel()
        await asyncio.wait_for(execution, timeout=5)

    asyncio.run(interrupt_worker())

    task = client.get(f"/tasks/{task_id}").json()
    assert task["status"] == "failed"
    assert "interrupted" in task["error_summary"].lower()
    events = client.get(f"/tasks/{task_id}/events").json()
    assert any(event["event_type"] == "executor.interrupted" for event in events)
    assert events[-1]["payload"]["terminal_reason"] == "worker_interrupted"



def test_workspace_snapshot_distinguishes_preexisting_and_new_changes(tmp_path):
    workspace = tmp_path / "git-workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.name", "AI Work OS tests"], check=True)
    tracked = workspace / "tracked.txt"
    tracked.write_text("committed\\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "initial"], check=True)

    # This modification predates the task and must be clearly labelled as such.
    tracked.write_text("pre-existing edit\\n", encoding="utf-8")
    before = asyncio.run(worker.capture_workspace_snapshot(workspace))
    assert before["available"] is True
    assert "tracked.txt" in before["tracked"]

    tracked.write_text("changed by task\\n", encoding="utf-8")
    (workspace / "new-output.txt").write_text("created during task\\n", encoding="utf-8")
    after = asyncio.run(worker.capture_workspace_snapshot(workspace))
    report = worker.compare_workspace_snapshots(before, after)

    assert report["available"] is True
    assert report["changed_during_task"] == ["new-output.txt", "tracked.txt"]
    assert report["preexisting_changes"] == ["tracked.txt"]
    assert report["final_changes"] == ["new-output.txt", "tracked.txt"]


def test_workspace_snapshot_reports_non_git_workspace_as_unavailable(tmp_path):
    snapshot = asyncio.run(worker.capture_workspace_snapshot(tmp_path))
    report = worker.compare_workspace_snapshots(snapshot, snapshot)
    assert snapshot["available"] is False
    assert report["available"] is False



def test_workspace_changes_endpoint_returns_report_and_handles_pending_report(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "STORAGE", tmp_path / "storage")
    created = client.post("/tasks", json={
        "title": "Workspace report API",
        "prompt": "Inspect changes",
        "workspace_path": str(tmp_path),
    })
    task_id = created.json()["id"]

    pending = client.get(f"/tasks/{task_id}/workspace-changes")
    assert pending.status_code == 200
    assert pending.json()["available"] is False

    report_path = api_module.STORAGE / "tasks" / task_id / "workspace_changes.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(
        '{"available": true, "changed_during_task": ["src/app.py"], '
        '"preexisting_changes": ["README.md"], "final_changes": ["README.md", "src/app.py"]}',
        encoding="utf-8",
    )
    response = client.get(f"/tasks/{task_id}/workspace-changes")
    assert response.status_code == 200
    assert response.json()["changed_during_task"] == ["src/app.py"]
    assert response.json()["preexisting_changes"] == ["README.md"]


def test_workspace_changes_endpoint_rejects_unknown_task():
    response = client.get("/tasks/not-a-real-task/workspace-changes")
    assert response.status_code == 404
