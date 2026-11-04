import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, timezone
from fastapi.testclient import TestClient
from ai_work_os.api import app
from ai_work_os import api as api_module
from ai_work_os import worker
from ai_work_os.db import SessionLocal
from ai_work_os.models import Task, TaskEvent, Workflow, utcnow

client = TestClient(app)


def utc_timestamp(value):
    """SQLite drops tzinfo, returning naive datetimes that represent UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_dashboard_task_selection_is_scoped_to_task_rows():
    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    # Workflow rows also use the shared .task styling class, but have no task data-id.
    assert 'data-workflow-id="' in html
    assert '$("tasks").querySelectorAll(".task[data-id]")' in html
    # A global selector would bind workflow rows and can request /tasks/undefined.
    assert 'document.querySelectorAll(".task")' not in html


def test_dashboard_is_served():
    response = client.get("/")
    assert response.status_code == 200
    assert "AI Work OS" in response.text
    assert "function renderTaskResult(events)" in response.text
    assert "Agent result" in response.text
    assert "function renderArtifacts(data)" in response.text
    assert "Workspace files changed during task" in response.text
    assert '$("tasks").querySelectorAll(".task[data-id]")' in response.text
    assert "selectedWorkflow" in response.text
    assert "workflowDetailToken" in response.text
    assert "if(selectedWorkflow)await loadWorkflowDetail(selectedWorkflow)" in response.text


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
    attempt_log = tmp_path / "storage" / "tasks" / task_id / "stdout.attempt-001.jsonl"
    assert attempt_log.is_file()
    assert (attempt_log.parent / "stdout.jsonl").is_file()
    events = client.get(f"/tasks/{task_id}/events").json()
    assert events[-1]["event_type"] == "executor.finished"
    assert events[-1]["payload"]["status"] == "failed"


def test_worker_restart_marks_orphaned_running_tasks_failed_without_retry(tmp_path):
    created = client.post("/tasks", json={
        "title": "Orphan recovery test", "prompt": "Do not replay automatically",
        "workspace_path": str(tmp_path), "max_attempts": 3,
    })
    task_id = created.json()["id"]
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        task.status = "running"
        task.attempt_count = 1
        task.started_at = utcnow() - timedelta(seconds=120)
        db.commit()

    recovered = worker.recover_orphaned_running_tasks()
    assert recovered >= 1
    task = client.get(f"/tasks/{task_id}").json()
    assert task["status"] == "failed"
    assert task["finished_at"] is not None
    assert task["exit_code"] == 125
    assert "not retried automatically" in task["error_summary"]

    events = client.get(f"/tasks/{task_id}/events").json()
    recovery = [event for event in events if event["event_type"] == "worker.orphaned_task_recovered"]
    assert len(recovery) == 1
    assert recovery[0]["payload"]["automatic_retry"] is False


def test_priority_and_retry_options_are_validated_and_persisted(tmp_path):
    created = client.post("/tasks", json={
        "title": "Prioritized task", "prompt": "Inspect", "workspace_path": str(tmp_path),
        "priority": 75, "max_attempts": 3,
    })
    assert created.status_code == 201
    task = created.json()
    assert task["priority"] == 75
    assert task["max_attempts"] == 3
    assert task["attempt_count"] == 0
    assert task["available_at"] is not None

    invalid = client.post("/tasks", json={
        "title": "Invalid priority", "prompt": "Inspect", "workspace_path": str(tmp_path),
        "priority": 101,
    })
    assert invalid.status_code == 422
    invalid_attempts = client.post("/tasks", json={
        "title": "Too many attempts", "prompt": "Inspect", "workspace_path": str(tmp_path),
        "max_attempts": 4,
    })
    assert invalid_attempts.status_code == 422


def test_priority_queue_claims_highest_priority_first(tmp_path):
    from ai_work_os.worker import claim_task
    with SessionLocal() as db:
        db.query(Task).filter(Task.status == "queued").update({Task.status: "cancelled"})
        db.commit()
    low = client.post("/tasks", json={
        "title": "Low priority", "prompt": "Inspect", "workspace_path": str(tmp_path), "priority": 1,
    }).json()
    high = client.post("/tasks", json={
        "title": "High priority", "prompt": "Inspect", "workspace_path": str(tmp_path), "priority": 90,
    }).json()
    assert claim_task() == high["id"]
    with SessionLocal() as db:
        low_task = db.get(Task, low["id"])
        assert low_task.status == "queued"
        claimed = db.get(Task, high["id"])
        assert claimed.attempt_count == 1
        low_task.status = "cancelled"
        claimed.status = "cancelled"
        db.commit()


def test_retry_is_scheduled_after_a_failed_attempt(tmp_path, monkeypatch):
    from ai_work_os.worker import claim_task
    with SessionLocal() as db:
        db.query(Task).filter(Task.status == "queued").update({Task.status: "cancelled"})
        db.commit()
    created = client.post("/tasks", json={
        "title": "Retry test", "prompt": "Retry if failed", "workspace_path": str(tmp_path),
        "max_attempts": 2,
    })
    task_id = created.json()["id"]
    assert claim_task() == task_id
    monkeypatch.setattr(worker, "CLAUDE_BIN", str(tmp_path / "missing-claude"))
    monkeypatch.setattr(worker, "STORAGE", tmp_path / "storage")
    monkeypatch.setattr(worker, "RETRY_BASE_DELAY_SECONDS", 0)
    asyncio.run(worker.execute_task(task_id))
    task = client.get(f"/tasks/{task_id}").json()
    assert task["status"] == "queued"
    assert task["attempt_count"] == 1
    assert task["finished_at"] is None
    events = client.get(f"/tasks/{task_id}/events").json()
    assert any(event["event_type"] == "executor.retry_scheduled" for event in events)
    assert (tmp_path / "storage" / "tasks" / task_id / "stdout.attempt-001.jsonl").is_file()

    assert claim_task() == task_id
    asyncio.run(worker.execute_task(task_id))
    task = client.get(f"/tasks/{task_id}").json()
    assert task["status"] == "failed"
    assert task["attempt_count"] == 2
    task_dir = tmp_path / "storage" / "tasks" / task_id
    assert (task_dir / "stdout.attempt-001.jsonl").is_file()
    assert (task_dir / "stdout.attempt-002.jsonl").is_file()
    assert (task_dir / "stdout.jsonl").read_text(encoding="utf-8") == (task_dir / "stdout.attempt-002.jsonl").read_text(encoding="utf-8")


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
    tracked.write_text("committed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "initial"], check=True)

    # This modification predates the task and must be clearly labelled as such.
    tracked.write_text("pre-existing edit\n", encoding="utf-8")
    before = asyncio.run(worker.capture_workspace_snapshot(workspace))
    assert before["available"] is True
    assert "tracked.txt" in before["tracked"]

    tracked.write_text("changed by task\n", encoding="utf-8")
    (workspace / "new-output.txt").write_text("created during task\n", encoding="utf-8")
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



def test_workspace_diff_endpoint_returns_tracked_and_untracked_diffs(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.name", "AI Work OS tests"], check=True)
    tracked = workspace / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "initial"], check=True)
    tracked.write_text("after\n", encoding="utf-8")
    (workspace / "new-file.txt").write_text("new file\n", encoding="utf-8")

    monkeypatch.setattr(api_module, "STORAGE", tmp_path / "storage")
    created = client.post("/tasks", json={
        "title": "Workspace diff API",
        "prompt": "Inspect file changes",
        "workspace_path": str(workspace),
    })
    assert created.status_code == 201
    task_id = created.json()["id"]
    report_path = api_module.STORAGE / "tasks" / task_id / "workspace_changes.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps({
        "available": True,
        "repository_root": str(workspace),
        "changed_during_task": ["tracked.txt", "new-file.txt"],
        "preexisting_changes": ["tracked.txt"],
        "final_changes": ["tracked.txt", "new-file.txt"],
        "changes_reverted_or_removed": [],
    }), encoding="utf-8")

    tracked_response = client.get(f"/tasks/{task_id}/workspace-diff", params={"path": "tracked.txt"})
    assert tracked_response.status_code == 200
    assert "-before" in tracked_response.json()["diff"]
    assert "+after" in tracked_response.json()["diff"]
    assert "preexisting_changes" in tracked_response.json()["categories"]

    untracked_response = client.get(f"/tasks/{task_id}/workspace-diff", params={"path": "new-file.txt"})
    assert untracked_response.status_code == 200
    assert "+new file" in untracked_response.json()["diff"]

    traversal = client.get(f"/tasks/{task_id}/workspace-diff", params={"path": "../outside.txt"})
    assert traversal.status_code == 400
    unknown = client.get(f"/tasks/{task_id}/workspace-diff", params={"path": "secret.txt"})
    assert unknown.status_code == 404


def test_workspace_diff_endpoint_reports_pending_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "STORAGE", tmp_path / "storage")
    created = client.post("/tasks", json={
        "title": "Pending workspace diff",
        "prompt": "Wait for report",
        "workspace_path": str(tmp_path),
    })
    response = client.get(
        f"/tasks/{created.json()['id']}/workspace-diff",
        params={"path": "main.py"},
    )
    assert response.status_code == 409


def test_workspace_changes_endpoint_rejects_unknown_task():
    response = client.get("/tasks/not-a-real-task/workspace-changes")
    assert response.status_code == 404


def test_task_artifacts_list_and_download_logs_and_changed_workspace_files(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.name", "AI Work OS tests"], check=True)
    tracked = workspace / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "initial"], check=True)
    tracked.write_text("after\n", encoding="utf-8")
    generated = workspace / "generated.txt"
    generated.write_text("generated output\n", encoding="utf-8")

    monkeypatch.setattr(api_module, "STORAGE", tmp_path / "storage")
    created = client.post("/tasks", json={
        "title": "Artifact downloads",
        "prompt": "Create a file",
        "workspace_path": str(workspace),
    })
    assert created.status_code == 201
    task_id = created.json()["id"]
    task_dir = api_module.STORAGE / "tasks" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "stdout.jsonl").write_text('{"type":"result"}\n', encoding="utf-8")
    (task_dir / "stderr.log").write_text("warning\n", encoding="utf-8")
    (task_dir / "workspace_changes.json").write_text(json.dumps({
        "available": True,
        "repository_root": str(workspace),
        "changed_during_task": ["tracked.txt", "generated.txt"],
        "preexisting_changes": [],
        "final_changes": ["tracked.txt", "generated.txt"],
        "changes_reverted_or_removed": [],
    }), encoding="utf-8")

    listing = client.get(f"/tasks/{task_id}/artifacts")
    assert listing.status_code == 200
    payload = listing.json()
    names = {item["name"] for item in payload["artifacts"]}
    assert names == {"stdout.jsonl", "stderr.log", "workspace_changes.json"}
    assert {item["path"] for item in payload["workspace_files"]} == {"tracked.txt", "generated.txt"}

    attempt_log_path = task_dir / "stdout.attempt-001.jsonl"
    attempt_log_path.write_text('{"type":"result","attempt":1}\n', encoding="utf-8")
    listing = client.get(f"/tasks/{task_id}/artifacts")
    names = {item["name"] for item in listing.json()["artifacts"]}
    assert "stdout.attempt-001.jsonl" in names
    attempt_download = client.get(f"/tasks/{task_id}/artifacts/stdout.attempt-001.jsonl")
    assert attempt_download.status_code == 200
    assert '"attempt":1' in attempt_download.text

    log = client.get(f"/tasks/{task_id}/artifacts/stdout.jsonl")
    assert log.status_code == 200
    assert '"type":"result"' in log.text
    report = client.get(f"/tasks/{task_id}/artifacts/workspace_changes.json")
    assert report.status_code == 200
    assert report.json()["available"] is True

    downloaded = client.get(
        f"/tasks/{task_id}/workspace-file",
        params={"path": "generated.txt"},
    )
    assert downloaded.status_code == 200
    assert downloaded.text == "generated output\n"

    traversal = client.get(
        f"/tasks/{task_id}/workspace-file",
        params={"path": "../outside.txt"},
    )
    assert traversal.status_code == 400
    unlisted = client.get(
        f"/tasks/{task_id}/workspace-file",
        params={"path": "tracked-secret.txt"},
    )
    assert unlisted.status_code == 404


def test_task_artifact_download_rejects_unknown_names_and_symlinks(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(api_module, "STORAGE", tmp_path / "storage")
    created = client.post("/tasks", json={
        "title": "Artifact path validation",
        "prompt": "Test artifact path safety",
        "workspace_path": str(workspace),
    })
    task_id = created.json()["id"]
    task_dir = api_module.STORAGE / "tasks" / task_id
    task_dir.mkdir(parents=True)
    outside = tmp_path / "outside.log"
    outside.write_text("secret", encoding="utf-8")
    (task_dir / "stderr.log").symlink_to(outside)

    unknown = client.get(f"/tasks/{task_id}/artifacts/../../outside.log")
    assert unknown.status_code == 404
    symlink = client.get(f"/tasks/{task_id}/artifacts/stderr.log")
    assert symlink.status_code == 404


def test_task_verification_passes_and_records_artifact(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_claude.chmod(0o755)
    monkeypatch.setattr(worker, "CLAUDE_BIN", str(fake_claude))
    monkeypatch.setattr(worker, "STORAGE", tmp_path / "storage")

    created = client.post("/tasks", json={
        "title": "Verification passes",
        "prompt": "Do work",
        "workspace_path": str(workspace),
        "acceptance_criteria": "The smoke check exits successfully",
        "verification_commands": [f'{sys.executable} -c "print(42)"'],
    })
    assert created.status_code == 201
    task_id = created.json()["id"]
    assert created.json()["verification_status"] == "pending"

    asyncio.run(worker.execute_task(task_id))

    task = client.get(f"/tasks/{task_id}").json()
    assert task["status"] == "succeeded"
    assert task["verification_status"] == "passed"
    events = client.get(f"/tasks/{task_id}/events").json()
    verification = [event for event in events if event["event_type"] == "verification.finished"]
    assert len(verification) == 1
    assert verification[0]["payload"]["status"] == "passed"
    artifact = tmp_path / "storage" / "tasks" / task_id / "verification.json"
    assert artifact.is_file()
    assert json.loads(artifact.read_text(encoding="utf-8"))["commands"][0]["exit_code"] == 0


def test_task_verification_failure_fails_task_without_retrying_agent(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_claude.chmod(0o755)
    monkeypatch.setattr(worker, "CLAUDE_BIN", str(fake_claude))
    monkeypatch.setattr(worker, "STORAGE", tmp_path / "storage")

    created = client.post("/tasks", json={
        "title": "Verification fails",
        "prompt": "Do work",
        "workspace_path": str(workspace),
        "max_attempts": 3,
        "verification_commands": [f'{sys.executable} -c "raise SystemExit(7)"'],
    })
    task_id = created.json()["id"]
    asyncio.run(worker.execute_task(task_id))

    task = client.get(f"/tasks/{task_id}").json()
    assert task["status"] == "failed"
    assert task["exit_code"] == 0
    assert task["verification_status"] == "failed"
    assert "Verification failed" in task["error_summary"]
    events = client.get(f"/tasks/{task_id}/events").json()
    finished = [event for event in events if event["event_type"] == "executor.finished"][-1]
    assert finished["payload"]["terminal_reason"] == "verification_failed"
    assert finished["payload"]["verification_status"] == "failed"
    assert not any(event["event_type"] == "executor.retry_scheduled" for event in events)


def test_verification_command_validation_and_default_status(tmp_path):
    created = client.post("/tasks", json={
        "title": "No verifier",
        "prompt": "Read only",
        "workspace_path": str(tmp_path),
    })
    assert created.status_code == 201
    assert created.json()["verification_status"] == "not_configured"
    invalid = client.post("/tasks", json={
        "title": "Too many checks",
        "prompt": "Read only",
        "workspace_path": str(tmp_path),
        "verification_commands": ["true"] * 6,
    })
    assert invalid.status_code == 422


def _write_fake_claude(path):
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'type': 'result', 'result': 'finished', 'session_id': 'fake-session'}))\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_auto_repair_options_are_opt_in_and_bounded(tmp_path):
    defaults = client.post("/tasks", json={
        "title": "Repair defaults", "prompt": "Inspect", "workspace_path": str(tmp_path),
    })
    assert defaults.status_code == 201
    assert defaults.json()["auto_repair"] is False
    assert defaults.json()["max_repair_attempts"] == 0
    assert defaults.json()["repair_attempt_count"] == 0

    enabled = client.post("/tasks", json={
        "title": "Repair enabled", "prompt": "Inspect", "workspace_path": str(tmp_path),
        "auto_repair": True, "max_repair_attempts": 2,
        "verification_commands": ["python -c \"print('ok')\""],
    })
    assert enabled.status_code == 201
    assert enabled.json()["auto_repair"] is True
    assert enabled.json()["max_repair_attempts"] == 2

    invalid = client.post("/tasks", json={
        "title": "Repair over cap", "prompt": "Inspect", "workspace_path": str(tmp_path),
        "auto_repair": True, "max_repair_attempts": 3,
    })
    assert invalid.status_code == 422


def test_bounded_repair_can_turn_failed_verification_into_success(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_claude = tmp_path / "fake-claude"
    _write_fake_claude(fake_claude)
    monkeypatch.setattr(worker, "CLAUDE_BIN", str(fake_claude))
    monkeypatch.setattr(worker, "STORAGE", tmp_path / "storage")

    created = client.post("/tasks", json={
        "title": "Repair success", "prompt": "Make checks pass",
        "workspace_path": str(workspace), "auto_repair": True,
        "max_repair_attempts": 1,
        "verification_commands": [f'"{sys.executable}" -c "import sys; sys.exit(1)"'],
    })
    assert created.status_code == 201
    task_id = created.json()["id"]
    calls = []

    async def fake_repair(task_id_arg, workspace_arg, permission_mode, prompt, task_dir, repair_number, max_repair_attempts, commands):
        calls.append((repair_number, commands))
        return True, [{
            "index": 1, "command": commands[0], "exit_code": 0,
            "timed_out": False, "stdout": "ok", "stderr": "",
        }], None

    monkeypatch.setattr(worker, "run_repair_attempt", fake_repair)
    asyncio.run(worker.execute_task(task_id))

    task = client.get(f"/tasks/{task_id}").json()
    assert task["status"] == "succeeded"
    assert task["verification_status"] == "passed"
    assert task["repair_attempt_count"] == 1
    assert len(calls) == 1
    assert calls[0][0] == 1
    assert calls[0][1] == created.json()["verification_commands"]


def test_bounded_repair_stops_at_configured_cap(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_claude = tmp_path / "fake-claude"
    _write_fake_claude(fake_claude)
    monkeypatch.setattr(worker, "CLAUDE_BIN", str(fake_claude))
    monkeypatch.setattr(worker, "STORAGE", tmp_path / "storage")

    created = client.post("/tasks", json={
        "title": "Repair cap", "prompt": "Make checks pass",
        "workspace_path": str(workspace), "auto_repair": True,
        "max_repair_attempts": 2,
        "verification_commands": [f'"{sys.executable}" -c "import sys; sys.exit(1)"'],
    })
    task_id = created.json()["id"]
    calls = []

    async def failing_repair(task_id_arg, workspace_arg, permission_mode, prompt, task_dir, repair_number, max_repair_attempts, commands):
        calls.append(repair_number)
        return False, [{
            "index": 1, "command": commands[0], "exit_code": 1,
            "timed_out": False, "stdout": "", "stderr": "still failing",
        }], None

    monkeypatch.setattr(worker, "run_repair_attempt", failing_repair)
    asyncio.run(worker.execute_task(task_id))

    task = client.get(f"/tasks/{task_id}").json()
    assert task["status"] == "failed"
    assert task["verification_status"] == "failed"
    assert task["repair_attempt_count"] == 2
    assert calls == [1, 2]


def test_disabled_repair_does_not_invoke_repair_agent(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_claude = tmp_path / "fake-claude"
    _write_fake_claude(fake_claude)
    monkeypatch.setattr(worker, "CLAUDE_BIN", str(fake_claude))
    monkeypatch.setattr(worker, "STORAGE", tmp_path / "storage")

    created = client.post("/tasks", json={
        "title": "Repair disabled", "prompt": "Make checks pass",
        "workspace_path": str(workspace),
        "verification_commands": [f'"{sys.executable}" -c "import sys; sys.exit(1)"'],
    })
    task_id = created.json()["id"]

    async def unexpected_repair(*args, **kwargs):
        raise AssertionError("repair must remain disabled by default")

    monkeypatch.setattr(worker, "run_repair_attempt", unexpected_repair)
    asyncio.run(worker.execute_task(task_id))

    task = client.get(f"/tasks/{task_id}").json()
    assert task["status"] == "failed"
    assert task["repair_attempt_count"] == 0


def test_repair_logs_are_listed_and_downloadable(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "STORAGE", tmp_path / "storage")
    created = client.post("/tasks", json={
        "title": "Repair log artifacts", "prompt": "Inspect", "workspace_path": str(tmp_path),
    })
    task_id = created.json()["id"]
    task_dir = api_module.STORAGE / "tasks" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "repair-01.stdout.jsonl").write_text('{"type":"result"}\n', encoding="utf-8")
    (task_dir / "repair-01.stderr.log").write_text("diagnostic\n", encoding="utf-8")

    artifacts = client.get(f"/tasks/{task_id}/artifacts").json()["artifacts"]
    names = {item["name"] for item in artifacts}
    assert "repair-01.stdout.jsonl" in names
    assert "repair-01.stderr.log" in names
    assert client.get(f"/tasks/{task_id}/artifacts/repair-01.stdout.jsonl").status_code == 200
    assert client.get(f"/tasks/{task_id}/artifacts/repair-01.stderr.log").status_code == 200
    assert client.get(f"/tasks/{task_id}/artifacts/repair-99.stdout.jsonl").status_code == 404


def test_cancellation_during_repair_closes_task_as_cancelled(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_claude = tmp_path / "fake-claude"
    _write_fake_claude(fake_claude)
    monkeypatch.setattr(worker, "CLAUDE_BIN", str(fake_claude))
    monkeypatch.setattr(worker, "STORAGE", tmp_path / "storage")

    created = client.post("/tasks", json={
        "title": "Cancel repair", "prompt": "Make checks pass",
        "workspace_path": str(workspace), "auto_repair": True,
        "max_repair_attempts": 1,
        "verification_commands": [f'"{sys.executable}" -c "import sys; sys.exit(1)"'],
    })
    task_id = created.json()["id"]

    async def cancel_during_repair(*args, **kwargs):
        response = client.post(f"/tasks/{task_id}/cancel")
        assert response.status_code == 200
        return False, [], "cancelled"

    monkeypatch.setattr(worker, "run_repair_attempt", cancel_during_repair)
    asyncio.run(worker.execute_task(task_id))

    task = client.get(f"/tasks/{task_id}").json()
    assert task["status"] == "cancelled"
    assert task["repair_attempt_count"] == 1
    events = client.get(f"/tasks/{task_id}/events").json()
    finished = [event for event in events if event["event_type"] == "executor.finished"][-1]
    assert finished["payload"]["terminal_reason"] == "cancelled"


def test_repair_runner_executes_agent_and_reruns_verification(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_claude = tmp_path / "fake-claude"
    _write_fake_claude(fake_claude)
    monkeypatch.setattr(worker, "CLAUDE_BIN", str(fake_claude))
    monkeypatch.setattr(worker, "STORAGE", tmp_path / "storage")
    created = client.post("/tasks", json={
        "title": "Repair runner success", "prompt": "Inspect",
        "workspace_path": str(workspace),
    })
    task_id = created.json()["id"]
    task_dir = tmp_path / "storage" / "tasks" / task_id
    task_dir.mkdir(parents=True)
    commands = [f'"{sys.executable}" -c "print(42)"']

    passed, results, error = asyncio.run(worker.run_repair_attempt(
        task_id, workspace, "plan", "Fix the failing check", task_dir, 1, 1, commands,
    ))

    assert passed is True
    assert error is None
    assert results[0]["exit_code"] == 0
    assert (task_dir / "repair-01.stdout.jsonl").is_file()
    assert (task_dir / "repair-01.stderr.log").is_file()
    events = client.get(f"/tasks/{task_id}/events").json()
    repair_events = [event["event_type"] for event in events if event["event_type"].startswith("repair.")]
    assert repair_events == ["repair.started", "repair.finished"]


def test_repair_runner_reports_failed_verification(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_claude = tmp_path / "fake-claude"
    _write_fake_claude(fake_claude)
    monkeypatch.setattr(worker, "CLAUDE_BIN", str(fake_claude))
    monkeypatch.setattr(worker, "STORAGE", tmp_path / "storage")
    created = client.post("/tasks", json={
        "title": "Repair runner failure", "prompt": "Inspect",
        "workspace_path": str(workspace),
    })
    task_id = created.json()["id"]
    task_dir = tmp_path / "storage" / "tasks" / task_id
    task_dir.mkdir(parents=True)
    commands = [f'"{sys.executable}" -c "import sys; sys.exit(9)"']

    passed, results, error = asyncio.run(worker.run_repair_attempt(
        task_id, workspace, "configured", "Fix the failing check", task_dir, 1, 1, commands,
    ))

    assert passed is False
    assert error is None
    assert results[0]["exit_code"] == 9
    events = client.get(f"/tasks/{task_id}/events").json()
    finished = [event for event in events if event["event_type"] == "repair.finished"][-1]
    assert finished["payload"]["status"] == "verification_failed"


def test_repair_started_event_uses_task_limit(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_claude = tmp_path / "fake-claude"
    _write_fake_claude(fake_claude)
    monkeypatch.setattr(worker, "CLAUDE_BIN", str(fake_claude))
    monkeypatch.setattr(worker, "STORAGE", tmp_path / "storage")
    created = client.post("/tasks", json={
        "title": "Repair event limit", "prompt": "Inspect",
        "workspace_path": str(workspace),
    })
    task_id = created.json()["id"]
    task_dir = tmp_path / "storage" / "tasks" / task_id
    task_dir.mkdir(parents=True)

    passed, _, _ = asyncio.run(worker.run_repair_attempt(
        task_id, workspace, "plan", "Fix", task_dir, 1, 1,
        [f'"{sys.executable}" -c "print(1)"'],
    ))
    assert passed is True
    events = client.get(f"/tasks/{task_id}/events").json()
    started = [event for event in events if event["event_type"] == "repair.started"][-1]
    assert started["payload"]["max_repair_attempts"] == 1


def test_cancelled_repair_preserves_initial_verification_output(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_claude = tmp_path / "fake-claude"
    _write_fake_claude(fake_claude)
    monkeypatch.setattr(worker, "CLAUDE_BIN", str(fake_claude))
    monkeypatch.setattr(worker, "STORAGE", tmp_path / "storage")
    created = client.post("/tasks", json={
        "title": "Cancelled repair artifact", "prompt": "Make checks pass",
        "workspace_path": str(workspace), "auto_repair": True,
        "max_repair_attempts": 1,
        "verification_commands": [
            f'"{sys.executable}" -c "print(\'initial output\'); import sys; sys.exit(1)"'
        ],
    })
    task_id = created.json()["id"]

    async def cancel_repair(*args, **kwargs):
        client.post(f"/tasks/{task_id}/cancel")
        return False, [], "cancelled"

    monkeypatch.setattr(worker, "run_repair_attempt", cancel_repair)
    asyncio.run(worker.execute_task(task_id))

    task = client.get(f"/tasks/{task_id}").json()
    assert task["status"] == "cancelled"
    assert task["verification_status"] == "not_run"
    artifact = json.loads((tmp_path / "storage" / "tasks" / task_id / "verification.json").read_text())
    assert artifact["status"] == "cancelled"
    assert artifact["commands"][0]["stdout"].strip() == "initial output"
    assert artifact["commands"][0]["exit_code"] == 1


def test_workspace_snapshot_ignores_python_cache_noise(tmp_path):
    import subprocess
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", str(workspace)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.name", "Test"], check=True)
    (workspace / "tracked.txt").write_text("base", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-m", "base"], check=True, capture_output=True)
    cache = workspace / "__pycache__"
    cache.mkdir()
    (cache / "generated.pyc").write_bytes(b"cache")
    (workspace / "real-change.txt").write_text("actual", encoding="utf-8")

    snapshot = asyncio.run(worker.capture_workspace_snapshot(workspace))
    assert snapshot["available"] is True
    assert "real-change.txt" in snapshot["untracked"]
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in snapshot["untracked"])


def _quiesce_unrelated_queued(task_ids):
    """Keep queued tasks from earlier tests out of scheduler assertions."""
    with SessionLocal() as db:
        db.query(Task).filter(Task.status == "queued", Task.id.notin_(list(task_ids))).update(
            {Task.status: "cancelled", Task.finished_at: utcnow()}, synchronize_session=False
        )
        db.commit()


def _workflow_payload(workspace, *, with_dependency=True):
    tasks = [
        {"key": "prepare", "title": "Prepare", "prompt": "prepare", "workspace_path": str(workspace), "priority": 100},
        {"key": "build", "title": "Build", "prompt": "build", "workspace_path": str(workspace),
         "depends_on": ["prepare"] if with_dependency else [], "priority": 99},
    ]
    return {"title": "API workflow", "tasks": tasks}


def test_create_workflow_and_dependency_gates_worker(tmp_path):
    response = client.post("/workflows", json=_workflow_payload(tmp_path))
    assert response.status_code == 201, response.text
    workflow = response.json()
    assert workflow["status"] == "queued"
    assert len(workflow["dependencies"]) == 1
    task_ids = {item["key"]: item["task"]["id"] for item in workflow["tasks"]}

    # Keep unrelated queued tasks from earlier API tests out of this scheduler assertion.
    with SessionLocal() as db:
        for unrelated in db.query(Task).filter(Task.status == "queued").all():
            if unrelated.id in task_ids.values():
                continue
            unrelated.status = "cancelled"
            unrelated.finished_at = utcnow()
        db.commit()

    # The first task is eligible; the dependent task must remain blocked from claim.
    assert worker.claim_task("worker-test") == task_ids["prepare"]
    assert worker.claim_task("worker-test") is None

    with SessionLocal() as db:
        first = db.get(Task, task_ids["prepare"])
        first.status = "succeeded"
        first.finished_at = utcnow()
        db.commit()

    assert worker.claim_task("worker-test") == task_ids["build"]


def test_workflow_rejects_unknown_dependency_and_cycle(tmp_path):
    payload = _workflow_payload(tmp_path)
    payload["tasks"][0]["depends_on"] = ["missing"]
    response = client.post("/workflows", json=payload)
    assert response.status_code == 422

    payload = _workflow_payload(tmp_path)
    payload["tasks"][0]["depends_on"] = ["build"]
    response = client.post("/workflows", json=payload)
    assert response.status_code == 422


def test_failed_parent_blocks_all_descendants(tmp_path):
    response = client.post("/workflows", json=_workflow_payload(tmp_path))
    assert response.status_code == 201
    workflow_id = response.json()["id"]
    task_ids = {item["key"]: item["task"]["id"] for item in response.json()["tasks"]}
    with SessionLocal() as db:
        first = db.get(Task, task_ids["prepare"])
        first.status = "failed"
        first.finished_at = utcnow()
        db.commit()

    worker.reconcile_workflows()
    detail = client.get(f"/workflows/{workflow_id}").json()
    states = {task["id"]: task["status"] for task in detail["tasks"]}
    assert states[task_ids["prepare"]] == "failed"
    assert states[task_ids["build"]] == "blocked"
    assert detail["status"] == "failed"
    blocked = next(task for task in detail["tasks"] if task["id"] == task_ids["build"])
    assert "prerequisite task" in blocked["blocked_reason"]


def test_workflow_cancel_cancels_queued_tasks(tmp_path):
    response = client.post("/workflows", json=_workflow_payload(tmp_path))
    assert response.status_code == 201
    workflow_id = response.json()["id"]
    cancelled = client.post(f"/workflows/{workflow_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    detail = client.get(f"/workflows/{workflow_id}").json()
    assert all(task["status"] == "cancelled" for task in detail["tasks"])


def test_workflow_cancel_requests_running_worker_shutdown_without_releasing_lease(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text("#!/bin/sh\nexec sleep 10\n", encoding="utf-8")
    fake_claude.chmod(0o755)
    monkeypatch.setattr(worker, "CLAUDE_BIN", str(fake_claude))
    monkeypatch.setattr(worker, "STORAGE", tmp_path / "storage")

    created = client.post("/workflows", json=_workflow_payload(workspace))
    assert created.status_code == 201
    workflow_id = created.json()["id"]
    task_id = next(item["task"]["id"] for item in created.json()["tasks"] if item["key"] == "prepare")

    with SessionLocal() as db:
        for other in db.query(Task).filter(Task.status == "queued").all():
            if other.id != task_id:
                other.status = "cancelled"
                other.finished_at = utcnow()
        db.commit()

    # Keep this test scoped to cancellation of one running workflow task;
    # unrelated workflow fixtures are reconciled in their own tests.
    monkeypatch.setattr(worker, "reconcile_workflows", lambda: None)
    worker_id = "workflow-cancel-worker"
    assert worker.claim_task(worker_id) == task_id

    async def execute_then_cancel_workflow():
        execution = asyncio.create_task(worker.execute_task(task_id, worker_id))
        for _ in range(100):
            events = client.get(f"/tasks/{task_id}/events").json()
            if any(event["event_type"] == "executor.started" for event in events):
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("worker execution did not start")

        cancelled = client.post(f"/workflows/{workflow_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"

        # Cancellation is a request to the live owner, not permission to steal
        # or clear its lease before the subprocess has actually stopped.
        with SessionLocal() as db:
            current = db.get(Task, task_id)
            assert current.status == "running"
            assert bool(current.cancel_requested)
            assert current.worker_id == worker_id
            assert current.lease_expires_at is not None

        await asyncio.wait_for(execution, timeout=5)

    asyncio.run(execute_then_cancel_workflow())

    with SessionLocal() as db:
        current = db.get(Task, task_id)
        assert current.status == "cancelled"
        assert current.worker_id is None
        assert current.lease_expires_at is None
    events = client.get(f"/tasks/{task_id}/events").json()
    assert any(event["event_type"] == "task.cancellation_requested" for event in events)
    assert events[-1]["event_type"] == "executor.finished"
    assert events[-1]["payload"]["status"] == "cancelled"


def test_multi_parent_workflow_waits_for_every_success_and_retry(tmp_path):
    payload = {
        "title": "Multi-parent workflow",
        "tasks": [
            {"key": "first", "title": "First", "prompt": "first", "workspace_path": str(tmp_path), "priority": 100},
            {"key": "second", "title": "Second", "prompt": "second", "workspace_path": str(tmp_path), "priority": 99},
            {"key": "join", "title": "Join", "prompt": "join", "workspace_path": str(tmp_path),
             "depends_on": ["first", "second"], "priority": 98},
        ],
    }
    response = client.post("/workflows", json=payload)
    assert response.status_code == 201, response.text
    workflow = response.json()
    ids = {item["key"]: item["task"]["id"] for item in workflow["tasks"]}

    with SessionLocal() as db:
        for unrelated in db.query(Task).filter(Task.status == "queued").all():
            if unrelated.id not in ids.values():
                unrelated.status = "cancelled"
                unrelated.finished_at = utcnow()
        db.commit()

    assert worker.claim_task("worker-test") == ids["first"]
    assert worker.claim_task("worker-test") == ids["second"]
    assert worker.claim_task("worker-test") is None

    with SessionLocal() as db:
        first = db.get(Task, ids["first"])
        second = db.get(Task, ids["second"])
        first.status = "succeeded"
        first.finished_at = utcnow()
        # Simulate the second parent waiting for its retry backoff.
        second.status = "queued"
        second.available_at = utcnow() + timedelta(hours=1)
        db.commit()

    assert worker.claim_task("worker-test") is None
    with SessionLocal() as db:
        second = db.get(Task, ids["second"])
        second.status = "succeeded"
        second.finished_at = utcnow()
        db.commit()

    assert worker.claim_task("worker-test") == ids["join"]
    with SessionLocal() as db:
        joined = db.get(Task, ids["join"])
        joined.status = "succeeded"
        joined.finished_at = utcnow()
        db.commit()
    worker.reconcile_workflows()
    assert client.get(f"/workflows/{workflow['id']}").json()["status"] == "succeeded"

def test_reconcile_later_failure_after_workflow_already_failed(tmp_path):
    payload = {
        "title": "Independent branches",
        "tasks": [
            {"key": "fail-fast", "title": "Fail fast", "prompt": "x", "workspace_path": str(tmp_path), "priority": 100},
            {"key": "slow-parent", "title": "Slow parent", "prompt": "x", "workspace_path": str(tmp_path), "priority": 99},
            {"key": "slow-child", "title": "Slow child", "prompt": "x", "workspace_path": str(tmp_path),
             "depends_on": ["slow-parent"], "priority": 98},
        ],
    }
    response = client.post("/workflows", json=payload)
    assert response.status_code == 201, response.text
    workflow_id = response.json()["id"]
    ids = {item["key"]: item["task"]["id"] for item in response.json()["tasks"]}
    with SessionLocal() as db:
        db.query(Task).filter(Task.status == "queued").update(
            {Task.status: "cancelled"}, synchronize_session=False
        )
        for key in ("slow-parent", "slow-child"):
            task = db.get(Task, ids[key])
            task.status = "queued"
            task.finished_at = None
        db.get(Task, ids["fail-fast"]).status = "failed"
        db.commit()

    # A failure in one branch must not finalize the workflow while siblings can still run.
    worker.reconcile_workflows()
    assert client.get(f"/workflows/{workflow_id}").json()["status"] in {"queued", "running"}
    with SessionLocal() as db:
        parent = db.get(Task, ids["slow-parent"])
        parent.status = "failed"
        parent.finished_at = utcnow()
        db.commit()

    worker.reconcile_workflows()
    detail = client.get(f"/workflows/{workflow_id}").json()
    states = {task["id"]: task["status"] for task in detail["tasks"]}
    assert states[ids["slow-child"]] == "blocked"
    assert detail["status"] == "failed"



def test_cancel_task_is_idempotent_and_emits_one_event(tmp_path):
    created = client.post("/tasks", json={
        "title": "Cancel idempotency", "prompt": "Inspect", "workspace_path": str(tmp_path),
    })
    task_id = created.json()["id"]
    assert client.post(f"/tasks/{task_id}/cancel").status_code == 200
    assert client.post(f"/tasks/{task_id}/cancel").status_code == 200
    events = client.get(f"/tasks/{task_id}/events").json()
    cancelled = [event for event in events if event["event_type"] == "task.cancelled"]
    assert len(cancelled) == 1


def test_worker_does_not_claim_task_from_cancelled_workflow(tmp_path):
    response = client.post("/workflows", json=_workflow_payload(tmp_path))
    assert response.status_code == 201
    workflow_id = response.json()["id"]
    task_id = response.json()["tasks"][0]["task"]["id"]
    with SessionLocal() as db:
        workflow = db.get(Workflow, workflow_id)
        workflow.status = "cancelled"
        workflow.finished_at = utcnow()
        # Deliberately leave the task queued to exercise the claim-side fence.
        db.commit()
    assert worker.claim_task("worker-test") != task_id
    with SessionLocal() as db:
        assert db.get(Task, task_id).status == "queued"


def _create_workflow_with_tasks(tmp_path, title, definitions):
    payload = {
        "title": title,
        "tasks": [
            {
                "key": key, "title": key, "prompt": "x", "workspace_path": str(tmp_path),
                "priority": 100 - index, "depends_on": depends_on,
            }
            for index, (key, depends_on) in enumerate(definitions)
        ],
    }
    response = client.post("/workflows", json=payload)
    assert response.status_code == 201, response.text
    created = response.json()
    ids = {item["key"]: item["task"]["id"] for item in created["tasks"]}
    _quiesce_unrelated_queued(ids.values())
    return created["id"], ids


def _workflow_states(workflow_id):
    detail = client.get(f"/workflows/{workflow_id}").json()
    return detail, {task["title"]: task for task in detail["tasks"]}


def test_cancelled_member_does_not_cancel_workflow_or_starve_others(tmp_path):
    workflow_id, ids = _create_workflow_with_tasks(tmp_path, "member cancel", [
        ("A", []), ("B", []), ("C", ["A"]),
    ])

    assert client.post(f"/tasks/{ids['A']}/cancel").status_code == 200
    worker.reconcile_workflows()

    detail, by_title = _workflow_states(workflow_id)
    assert by_title["A"]["status"] == "cancelled"
    assert by_title["C"]["status"] == "blocked"
    assert "cancelled" in by_title["C"]["blocked_reason"]
    assert ids["A"] in by_title["C"]["blocked_reason"]
    assert detail["status"] in {"queued", "running"}
    assert detail["status"] != "cancelled"

    # The regression: a cancelled member used to make the whole workflow terminal,
    # which starved every queued task behind the claim-side workflow fence.
    assert worker.claim_task("worker-cancel-member") == ids["B"]
    with SessionLocal() as db:
        task = db.get(Task, ids["B"])
        task.status = "succeeded"
        task.finished_at = utcnow()
        task.worker_id = None
        task.lease_expires_at = None
        db.commit()

    worker.reconcile_workflows()
    final = client.get(f"/workflows/{workflow_id}").json()
    assert final["status"] == "cancelled"
    assert final["counts"] == {"cancelled": 1, "blocked": 1, "succeeded": 1}


def test_cancelled_task_does_not_block_independent_branch(tmp_path):
    workflow_id, ids = _create_workflow_with_tasks(tmp_path, "independent branches", [
        ("X", []), ("Y", []),
    ])

    assert client.post(f"/tasks/{ids['X']}/cancel").status_code == 200
    worker.reconcile_workflows()
    assert worker.claim_task("worker-independent") == ids["Y"]
    with SessionLocal() as db:
        task = db.get(Task, ids["Y"])
        task.status = "succeeded"
        task.finished_at = utcnow()
        task.worker_id = None
        task.lease_expires_at = None
        db.commit()
    worker.reconcile_workflows()

    detail, by_title = _workflow_states(workflow_id)
    assert by_title["X"]["status"] == "cancelled"
    assert by_title["Y"]["status"] == "succeeded"
    assert detail["status"] == "cancelled"


def test_multi_level_cancellation_blocks_descendants_only(tmp_path):
    workflow_id, ids = _create_workflow_with_tasks(tmp_path, "multi level cancel", [
        ("A", []), ("B", ["A"]), ("C", ["B"]), ("D", []),
    ])

    assert client.post(f"/tasks/{ids['A']}/cancel").status_code == 200
    worker.reconcile_workflows()

    detail, by_title = _workflow_states(workflow_id)
    assert by_title["A"]["status"] == "cancelled"
    assert by_title["B"]["status"] == "blocked"
    assert by_title["C"]["status"] == "blocked"
    for key in ("B", "C"):
        assert "cancelled" in by_title[key]["blocked_reason"]
        assert ids["A"] in by_title[key]["blocked_reason"]
    assert by_title["D"]["status"] == "queued"
    assert detail["status"] in {"queued", "running"}

    assert worker.claim_task("worker-multi-level") == ids["D"]
    with SessionLocal() as db:
        task = db.get(Task, ids["D"])
        task.status = "succeeded"
        task.finished_at = utcnow()
        task.worker_id = None
        task.lease_expires_at = None
        db.commit()
    worker.reconcile_workflows()
    assert client.get(f"/workflows/{workflow_id}").json()["status"] == "cancelled"


def test_workflow_cancel_is_terminal_and_does_not_rewrite_events(tmp_path):
    workflow_id, ids = _create_workflow_with_tasks(tmp_path, "workflow cancel", [
        ("A", []), ("B", ["A"]), ("C", []),
    ])
    with SessionLocal() as db:
        task = db.get(Task, ids["B"])
        task.status = "running"
        task.started_at = utcnow()
        task.attempt_count = 1
        task.worker_id = "stale-worker"
        task.lease_expires_at = utcnow() + timedelta(seconds=60)
        db.commit()

    assert client.post(f"/workflows/{workflow_id}/cancel").status_code == 200
    detail, by_title = _workflow_states(workflow_id)
    assert detail["status"] == "cancelled"
    assert by_title["A"]["status"] == "cancelled"
    assert by_title["C"]["status"] == "cancelled"
    assert by_title["B"]["status"] == "running"
    assert by_title["B"]["cancel_requested"] == 1
    finished_at = detail["finished_at"]

    worker.reconcile_workflows()
    detail, by_title = _workflow_states(workflow_id)
    assert detail["status"] == "cancelled"
    assert detail["finished_at"] == finished_at
    assert by_title["B"]["status"] == "running"

    def event_counts():
        with SessionLocal() as db:
            return {
                "queued_cancelled": sum(
                    db.query(TaskEvent).filter(
                        TaskEvent.task_id == ids[key], TaskEvent.event_type == "task.cancelled",
                    ).count() for key in ("A", "C")
                ),
                "running_requests": db.query(TaskEvent).filter(
                    TaskEvent.task_id == ids["B"], TaskEvent.event_type == "task.cancellation_requested",
                ).count(),
            }

    baseline = event_counts()
    assert baseline == {"queued_cancelled": 2, "running_requests": 1}
    assert client.post(f"/workflows/{workflow_id}/cancel").status_code == 200
    worker.reconcile_workflows()
    assert event_counts() == baseline

    # A stale worker finishing late must not resurrect the workflow.
    with SessionLocal() as db:
        task = db.get(Task, ids["B"])
        task.status = "cancelled"
        task.finished_at = utcnow()
        task.worker_id = None
        task.lease_expires_at = None
        db.commit()
    worker.reconcile_workflows()
    detail, _ = _workflow_states(workflow_id)
    assert detail["status"] == "cancelled"
    assert detail["finished_at"] == finished_at


def test_concurrent_reconciliation_is_idempotent(tmp_path):
    workflow_id, ids = _create_workflow_with_tasks(tmp_path, "concurrent reconcile", [
        ("A", []), ("B", ["A"]), ("C", ["A"]),
    ])
    with SessionLocal() as db:
        task = db.get(Task, ids["A"])
        task.status = "failed"
        task.finished_at = utcnow()
        db.commit()

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: worker.reconcile_workflows(), range(6)))

    detail, by_title = _workflow_states(workflow_id)
    assert detail["status"] == "failed"
    assert by_title["B"]["status"] == "blocked"
    assert by_title["C"]["status"] == "blocked"
    with SessionLocal() as db:
        blocked_events = {
            key: db.query(TaskEvent).filter(
                TaskEvent.task_id == ids[key], TaskEvent.event_type == "task.blocked",
            ).count()
            for key in ("B", "C")
        }
    assert blocked_events == {"B": 1, "C": 1}

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: worker.reconcile_workflows(), range(6)))
    detail, by_title = _workflow_states(workflow_id)
    assert detail["status"] == "failed"
    assert by_title["B"]["status"] == "blocked"
    assert by_title["C"]["status"] == "blocked"
    with SessionLocal() as db:
        for key in ("B", "C"):
            assert db.query(TaskEvent).filter(
                TaskEvent.task_id == ids[key], TaskEvent.event_type == "task.blocked",
            ).count() == 1



def test_worker_claim_records_owner_and_lease(tmp_path):
    created = client.post("/tasks", json={
        "title": "Lease claim", "prompt": "Inspect", "workspace_path": str(tmp_path),
    })
    assert created.status_code == 201
    task_id = created.json()["id"]
    with SessionLocal() as db:
        for other in db.query(Task).filter(Task.status == "queued").all():
            if other.id != task_id:
                other.status = "cancelled"
                other.finished_at = utcnow()
        db.commit()

    worker_id = "worker-test-owner"
    assert worker.claim_task(worker_id) == task_id
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        assert task.worker_id == worker_id
        assert task.lease_expires_at is not None
        assert utc_timestamp(task.lease_expires_at) > utcnow().timestamp()


def test_multiple_workers_claim_distinct_tasks_with_independent_leases(tmp_path):
    first = client.post("/tasks", json={
        "title": "Worker A task", "prompt": "Inspect", "workspace_path": str(tmp_path),
    }).json()["id"]
    second = client.post("/tasks", json={
        "title": "Worker B task", "prompt": "Inspect", "workspace_path": str(tmp_path),
    }).json()["id"]
    with SessionLocal() as db:
        for other in db.query(Task).filter(Task.status == "queued").all():
            if other.id not in {first, second}:
                other.status = "cancelled"
                other.finished_at = utcnow()
        db.commit()

    claimed_a = worker.claim_task("worker-A")
    claimed_b = worker.claim_task("worker-B")
    assert {claimed_a, claimed_b} == {first, second}
    with SessionLocal() as db:
        task_a = db.get(Task, claimed_a)
        task_b = db.get(Task, claimed_b)
        assert task_a.worker_id == "worker-A"
        assert task_b.worker_id == "worker-B"
        assert task_a.lease_expires_at is not None
        assert task_b.lease_expires_at is not None


def test_simultaneous_workers_cannot_claim_the_same_task(tmp_path, monkeypatch):
    task_id = client.post("/tasks", json={
        "title": "Contended claim", "prompt": "Inspect", "workspace_path": str(tmp_path),
    }).json()["id"]
    with SessionLocal() as db:
        for other in db.query(Task).filter(Task.status == "queued").all():
            if other.id != task_id:
                other.status = "cancelled"
                other.finished_at = utcnow()
        db.commit()

    # Keep this test focused on the claim transaction, not workflow reconciliation.
    monkeypatch.setattr(worker, "reconcile_workflows", lambda: None)
    start = threading.Barrier(3)

    def claim(worker_id):
        start.wait(timeout=5)
        return worker.claim_task(worker_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(claim, "worker-race-A")
        second = pool.submit(claim, "worker-race-B")
        start.wait(timeout=5)
        results = [first.result(timeout=10), second.result(timeout=10)]

    assert results.count(task_id) == 1
    assert results.count(None) == 1
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        assert task.status == "running"
        assert task.worker_id in {"worker-race-A", "worker-race-B"}
        assert task.attempt_count == 1
        assert task.lease_expires_at is not None


def test_worker_heartbeat_renews_matching_lease(tmp_path, monkeypatch):
    created = client.post("/tasks", json={
        "title": "Lease heartbeat", "prompt": "Inspect", "workspace_path": str(tmp_path),
    })
    task_id = created.json()["id"]
    with SessionLocal() as db:
        for other in db.query(Task).filter(Task.status == "queued").all():
            if other.id != task_id:
                other.status = "cancelled"
                other.finished_at = utcnow()
        db.commit()
    worker_id = "worker-heartbeat"
    assert worker.claim_task(worker_id) == task_id
    monkeypatch.setattr(worker, "WORKER_HEARTBEAT_SECONDS", 0.01)
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        task.lease_expires_at = utcnow() + timedelta(seconds=1)
        db.commit()
    async def exercise():
        heartbeat = asyncio.create_task(worker._renew_worker_lease(task_id, worker_id))
        await asyncio.sleep(0.04)
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
    asyncio.run(exercise())
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        assert utc_timestamp(task.lease_expires_at) > (utcnow() + timedelta(seconds=30)).timestamp()


def test_recovery_only_fails_expired_leases_and_legacy_after_grace(tmp_path, monkeypatch):
    fresh = client.post("/tasks", json={
        "title": "Fresh lease", "prompt": "Inspect", "workspace_path": str(tmp_path),
    }).json()["id"]
    expired = client.post("/tasks", json={
        "title": "Expired lease", "prompt": "Inspect", "workspace_path": str(tmp_path),
    }).json()["id"]
    legacy = client.post("/tasks", json={
        "title": "Legacy lease", "prompt": "Inspect", "workspace_path": str(tmp_path),
    }).json()["id"]
    with SessionLocal() as db:
        for task_id in (fresh, expired, legacy):
            task = db.get(Task, task_id)
            task.status = "running"
            task.started_at = utcnow()
        db.get(Task, fresh).worker_id = "worker-live"
        db.get(Task, fresh).lease_expires_at = utcnow() + timedelta(minutes=1)
        db.get(Task, expired).worker_id = "worker-dead"
        db.get(Task, expired).lease_expires_at = utcnow() - timedelta(seconds=1)
        db.get(Task, legacy).worker_id = None
        db.get(Task, legacy).lease_expires_at = None
        db.get(Task, legacy).started_at = utcnow() - timedelta(seconds=120)
        db.commit()

    monkeypatch.setattr(worker, "WORKER_LEASE_SECONDS", 60)
    assert worker.recover_orphaned_running_tasks() == 2
    with SessionLocal() as db:
        assert db.get(Task, fresh).status == "running"
        for task_id in (expired, legacy):
            task = db.get(Task, task_id)
            assert task.status == "failed"
            assert task.exit_code == 125
            assert task.worker_id is None
            assert task.lease_expires_at is None


def test_direct_execution_cannot_steal_live_worker_lease(tmp_path):
    created = client.post("/tasks", json={
        "title": "Do not steal lease", "prompt": "Inspect", "workspace_path": str(tmp_path),
    })
    task_id = created.json()["id"]
    with SessionLocal() as db:
        for other in db.query(Task).filter(Task.status == "queued").all():
            if other.id != task_id:
                other.status = "cancelled"
                other.finished_at = utcnow()
        db.commit()

    assert worker.claim_task("worker-owner-A") == task_id
    assert worker.claim_task("worker-owner-B") is None
    asyncio.run(worker.execute_task(task_id))
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        assert task.status == "running"
        assert task.worker_id == "worker-owner-A"
        assert task.lease_expires_at is not None


def test_stale_worker_cannot_complete_reassigned_task(tmp_path, monkeypatch):
    created = client.post("/tasks", json={
        "title": "Fence stale worker", "prompt": "Inspect", "workspace_path": str(tmp_path),
    })
    task_id = created.json()["id"]
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        task.status = "running"
        task.worker_id = "worker-owner-B"
        task.lease_expires_at = utcnow() + timedelta(seconds=60)
        db.commit()

    monkeypatch.setattr(worker, "STORAGE", tmp_path / "storage")
    asyncio.run(worker._execute_task(task_id, "worker-owner-A"))
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        assert task.status == "running"
        assert task.worker_id == "worker-owner-B"
        assert task.exit_code is None


def test_expired_lease_recovery_is_idempotent(tmp_path):
    created = client.post("/tasks", json={
        "title": "Recover once", "prompt": "Inspect", "workspace_path": str(tmp_path),
    })
    task_id = created.json()["id"]
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        task.status = "running"
        task.worker_id = "worker-gone"
        task.started_at = utcnow() - timedelta(minutes=2)
        task.lease_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()

    assert worker.recover_orphaned_running_tasks() == 1
    assert worker.recover_orphaned_running_tasks() == 0
    events = client.get(f"/tasks/{task_id}/events").json()
    recovered = [event for event in events if event["event_type"] == "worker.orphaned_task_recovered"]
    assert len(recovered) == 1


def test_worker_cannot_resurrect_expired_lease(tmp_path, monkeypatch):
    created = client.post("/tasks", json={
        "title": "Expired heartbeat", "prompt": "Inspect", "workspace_path": str(tmp_path),
    })
    task_id = created.json()["id"]
    with SessionLocal() as db:
        for other in db.query(Task).filter(Task.status == "queued").all():
            if other.id != task_id:
                other.status = "cancelled"
                other.finished_at = utcnow()
        db.commit()
    worker_id = "monitor-expired-worker"
    assert worker.claim_task(worker_id) == task_id
    monkeypatch.setattr(worker, "WORKER_HEARTBEAT_SECONDS", 0.01)
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        task.lease_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()

    async def exercise():
        try:
            await worker._renew_worker_lease(task_id, worker_id)
        except RuntimeError as exc:
            assert "lease lost" in str(exc)
        else:
            raise AssertionError("expired lease must not be renewed")

    asyncio.run(exercise())
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        assert utc_timestamp(task.lease_expires_at) < utcnow().timestamp()


def test_cancel_during_verification_terminates_command_and_records_results(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_claude.chmod(0o755)
    monkeypatch.setattr(worker, "CLAUDE_BIN", str(fake_claude))
    monkeypatch.setattr(worker, "STORAGE", tmp_path / "storage")
    created = client.post("/tasks", json={
        "title": "Cancel verification",
        "prompt": "Run verification",
        "workspace_path": str(workspace),
        "verification_commands": [f'"{sys.executable}" -c "import time; time.sleep(30)"'],
    })
    assert created.status_code == 201
    task_id = created.json()["id"]

    async def execute_and_cancel():
        execution = asyncio.create_task(worker.execute_task(task_id))
        for _ in range(100):
            events = client.get(f"/tasks/{task_id}/events").json()
            if any(event["event_type"] == "verification.started" for event in events):
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("verification did not start")
        response = client.post(f"/tasks/{task_id}/cancel")
        assert response.status_code == 200
        await asyncio.wait_for(execution, timeout=5)

    asyncio.run(execute_and_cancel())
    task = client.get(f"/tasks/{task_id}").json()
    assert task["status"] == "cancelled"
    assert task["verification_status"] == "not_run"
    artifact = json.loads((tmp_path / "storage" / "tasks" / task_id / "verification.json").read_text())
    assert artifact["status"] == "cancelled"
    assert artifact["commands"][0]["cancelled"] is True

def test_workers_endpoint_reports_idle_offline_and_expired_lease(tmp_path):
    from datetime import timedelta
    from ai_work_os.models import Worker

    import uuid
    prefix = f"monitor-{uuid.uuid4().hex[:8]}"
    idle_id, offline_id, expired_id = (f"{prefix}-idle", f"{prefix}-offline", f"{prefix}-expired")
    now = utcnow()
    with SessionLocal() as db:
        db.add_all([
            Worker(worker_id=idle_id, hostname="host-a", pid=101,
                   started_at=now - timedelta(minutes=2), last_heartbeat_at=now,
                   status="idle", concurrency=2, active_task_count=0),
            Worker(worker_id=offline_id, hostname="host-b", pid=202,
                   started_at=now - timedelta(minutes=10),
                   last_heartbeat_at=now - timedelta(minutes=5),
                   status="busy", concurrency=1, active_task_count=1),
            Worker(worker_id=expired_id, hostname="host-c", pid=303,
                   started_at=now - timedelta(minutes=3), last_heartbeat_at=now,
                   status="busy", concurrency=1, active_task_count=1),
        ])
        db.add(Task(
            title="Expired worker task", prompt="test", workspace_path=str(tmp_path),
            status="running", worker_id=expired_id,
            started_at=now - timedelta(minutes=1),
            lease_expires_at=now - timedelta(seconds=1),
        ))
        db.commit()

    response = client.get("/workers")
    assert response.status_code == 200
    payload = response.json()
    by_id = {item["worker_id"]: item for item in payload["workers"]}
    assert by_id[idle_id]["status"] == "idle"
    assert by_id[offline_id]["status"] == "offline"
    assert by_id[expired_id]["status"] == "lease_expired"
    assert by_id[expired_id]["active_tasks"][0]["lease_expired"] is True
    assert payload["by_status"]["idle"] >= 1
    assert payload["by_status"]["offline"] >= 1
    assert payload["by_status"]["lease_expired"] >= 1


def test_worker_heartbeat_registers_process_and_tracks_active_tasks(tmp_path):
    from ai_work_os.models import Worker

    created = client.post("/tasks", json={
        "title": "Worker monitor task", "prompt": "inspect",
        "workspace_path": str(tmp_path),
    })
    task_id = created.json()["id"]
    import uuid
    worker_id = f"worker-heartbeat-{uuid.uuid4().hex[:8]}"
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        task.status = "running"
        task.worker_id = worker_id
        task.started_at = utcnow()
        task.lease_expires_at = utcnow() + timedelta(minutes=1)
        db.commit()

    worker.heartbeat_worker(worker_id, 3)
    with SessionLocal() as db:
        record = db.get(Worker, worker_id)
        assert record is not None
        assert record.status == "busy"
        assert record.active_task_count == 1
        assert record.concurrency == 3

    worker.mark_worker_offline(worker_id)
    with SessionLocal() as db:
        assert db.get(Worker, worker_id).status == "offline"
    listed = client.get("/workers").json()
    current = next(item for item in listed["workers"] if item["worker_id"] == worker_id)
    assert current["status"] == "offline"


def test_workflow_events_are_merged_chronologically_and_filter_token_noise(tmp_path):
    created = client.post("/workflows", json={
        "title": "Execution trace fixture",
        "tasks": [
            {"key": "prepare", "title": "Prepare workspace", "prompt": "Inspect", "workspace_path": str(tmp_path)},
            {"key": "verify", "title": "Verify result", "prompt": "Verify", "workspace_path": str(tmp_path),
             "depends_on": ["prepare"]},
        ],
    })
    assert created.status_code == 201
    workflow_id = created.json()["id"]
    task_ids = {item["key"]: item["task"]["id"] for item in created.json()["tasks"]}
    base = utcnow()
    with SessionLocal() as db:
        db.add_all([
            TaskEvent(task_id=task_ids["prepare"], occurred_at=base,
                      event_type="executor.claimed", payload={"attempt": 1}),
            TaskEvent(task_id=task_ids["verify"], occurred_at=base + timedelta(seconds=2),
                      event_type="executor.finished", payload={"status": "failed", "error_summary": "verification failed"}),
            TaskEvent(task_id=task_ids["verify"], occurred_at=base + timedelta(seconds=3),
                      event_type="claude.system", payload={"subtype": "thinking_tokens", "estimated_tokens": 42}),
        ])
        db.commit()

    response = client.get(f"/workflows/{workflow_id}/events")
    assert response.status_code == 200
    events = response.json()
    assert [event["event_type"] for event in events] == [
        "task.queued", "task.queued", "executor.claimed", "executor.finished",
    ]
    assert [event["occurred_at"] for event in events] == sorted(
        event["occurred_at"] for event in events
    )
    failure = events[-1]
    assert failure["task_id"] == task_ids["verify"]
    assert failure["task_title"] == "Verify result"
    assert failure["payload"]["error_summary"] == "verification failed"

    all_events = client.get(
        f"/workflows/{workflow_id}/events?include_thinking_tokens=true"
    ).json()
    assert any(event["payload"].get("subtype") == "thinking_tokens" for event in all_events)
    assert client.get("/workflows/missing-workflow/events").status_code == 404


def test_task_diagnostics_classifies_verification_failure_and_replays_events(tmp_path):
    created = client.post("/tasks", json={
        "title": "Diagnose verification failure",
        "prompt": "Implement then verify",
        "workspace_path": str(tmp_path),
        "verification_commands": ["python -m pytest -q"],
    })
    assert created.status_code == 201
    task_id = created.json()["id"]
    base = utcnow()
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        task.status = "failed"
        task.verification_status = "failed"
        task.exit_code = 1
        task.error_summary = "Verification failed for command: python -m pytest -q"
        db.add_all([
            TaskEvent(task_id=task_id, occurred_at=base + timedelta(seconds=2),
                      event_type="verification.finished",
                      payload={"status": "failed", "commands": [{"exit_code": 1}]}),
            TaskEvent(task_id=task_id, occurred_at=base + timedelta(seconds=1),
                      event_type="executor.claimed", payload={"attempt": 1}),
        ])
        db.commit()

    response = client.get(f"/tasks/{task_id}/diagnostics")
    assert response.status_code == 200
    report = response.json()
    assert report["classification"] == "verification_failed"
    assert report["confidence"] == "high"
    assert report["task"]["prompt"] == "Implement then verify"
    assert report["event_count"] >= 3
    assert [item["occurred_at"] for item in report["replay"]] == sorted(
        item["occurred_at"] for item in report["replay"]
    )
    assert all(item["evidence_type"] == "recorded_event" for item in report["replay"])
    assert report["hypotheses"][0]["evidence_event_ids"]
    assert "not proof of root cause" in report["limitations"][1]


def test_task_diagnostics_classifies_worker_loss_and_unknown_without_inference(tmp_path):
    worker_task = client.post("/tasks", json={
        "title": "Worker loss fixture", "prompt": "Inspect", "workspace_path": str(tmp_path),
    }).json()["id"]
    unknown_task = client.post("/tasks", json={
        "title": "Successful fixture", "prompt": "Inspect", "workspace_path": str(tmp_path),
    }).json()["id"]
    with SessionLocal() as db:
        lost = db.get(Task, worker_task)
        lost.status = "failed"
        lost.error_summary = "Worker lease expired"
        db.add(TaskEvent(task_id=worker_task, event_type="worker.orphaned_task_recovered",
                         payload={"automatic_retry": False}))
        db.get(Task, unknown_task).status = "succeeded"
        db.commit()

    lost_report = client.get(f"/tasks/{worker_task}/diagnostics").json()
    unknown_report = client.get(f"/tasks/{unknown_task}/diagnostics").json()
    assert lost_report["classification"] == "worker_lost"
    assert lost_report["confidence"] == "high"
    assert unknown_report["classification"] is None
    assert unknown_report["confidence"] == "unknown"
    assert unknown_report["hypotheses"] == []


def test_task_diagnostics_returns_404_for_missing_task():
    assert client.get("/tasks/missing-task/diagnostics").status_code == 404


def test_dashboard_exposes_failure_analysis_and_execution_replay_entry_points():
    response = client.get("/")
    assert response.status_code == 200
    assert '"/tasks/"+encodeURIComponent(id)+"/diagnostics"' in response.text
    assert "Failure analysis" in response.text
    assert "Execution replay" in response.text


def test_task_diagnostics_prioritizes_nested_verification_timeout(tmp_path):
    task_id = client.post("/tasks", json={
        "title": "Verification timeout fixture",
        "prompt": "Run checks",
        "workspace_path": str(tmp_path),
        "verification_commands": ["python -m pytest"],
    }).json()["id"]
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        task.status = "failed"
        task.verification_status = "failed"
        db.add(TaskEvent(
            task_id=task_id,
            event_type="verification.finished",
            payload={"status": "failed", "commands": [
                {"command": "python -m pytest", "exit_code": -15, "timed_out": True}
            ]},
        ))
        db.commit()

    report = client.get(f"/tasks/{task_id}/diagnostics").json()
    assert report["classification"] == "timeout"
    assert report["confidence"] == "high"
    assert report["hypotheses"][0]["evidence_event_ids"]


def test_execution_insights_aggregate_durations_and_reported_claude_usage(tmp_path):
    created = client.post("/tasks", json={
        "title": "Insights measured task",
        "prompt": "fixture",
        "workspace_path": str(tmp_path),
    })
    assert created.status_code == 201
    task_id = created.json()["id"]
    now = utcnow()
    started = now - timedelta(minutes=3)
    finished = now - timedelta(minutes=1)
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        task.status = "succeeded"
        task.created_at = now - timedelta(minutes=5)
        task.started_at = started
        task.finished_at = finished
        workflow = Workflow(
            title="Insights workflow", status="succeeded",
            created_at=now - timedelta(minutes=6),
            started_at=now - timedelta(minutes=4),
            finished_at=finished,
        )
        db.add(workflow)
        db.flush()
        task.workflow_id = workflow.id
        db.add(TaskEvent(task_id=task_id, event_type="claude.result", occurred_at=finished, payload={
            "total_cost_usd": 0.012345,
            "usage": {
                "input_tokens": 120,
                "output_tokens": 45,
                "cache_read_input_tokens": 30,
                "cache_creation_input_tokens": 10,
            },
        }))
        db.commit()

    response = client.get("/insights?period_days=30")
    assert response.status_code == 200
    data = response.json()
    assert data["tasks"]["total"] >= 1
    assert data["tasks"]["by_status"]["succeeded"] >= 1
    assert data["tasks"]["queue_wait_ms"]["samples"] >= 1
    assert data["tasks"]["execution_ms"]["samples"] >= 1
    assert data["tasks"]["end_to_end_ms"]["samples"] >= 1
    assert data["workflows"]["wall_clock_ms"]["samples"] >= 1
    assert data["workflows"]["cumulative_task_execution_ms"] >= 120000
    assert data["usage"]["cost_usd"] >= 0.012345
    assert data["usage"]["token_totals"]["input_tokens"] >= 120
    assert data["usage"]["token_totals"]["output_tokens"] >= 45
    assert data["usage"]["availability"] == "reported"
    assert data["workers"]["historical_utilization_available"] is False


def test_execution_insights_never_estimate_missing_cost_or_usage(tmp_path):
    created = client.post("/tasks", json={
        "title": "No usage metadata task",
        "prompt": "fixture",
        "workspace_path": str(tmp_path),
    })
    task_id = created.json()["id"]
    with SessionLocal() as db:
        # This module uses a shared test database. Move every prior fixture
        # outside the selected window so earlier usage events cannot affect
        # the assertion that an empty window reports no collected usage.
        cutoff = utcnow() - timedelta(days=8)
        for existing in db.query(Task).all():
            existing.created_at = cutoff
        db.add(TaskEvent(task_id=task_id, event_type="claude.result", payload={
            "result": "completed without usage metadata",
        }))
        db.commit()

    response = client.get("/insights?period_days=7")
    assert response.status_code == 200
    data = response.json()
    assert data["usage"]["cost_usd"] is None
    assert data["usage"]["tasks_with_reported_cost"] == 0
    assert data["usage"]["tasks_with_reported_usage"] == 0
    assert data["usage"]["availability"] == "not_collected"


def test_session_inventory_and_detail_expose_real_capabilities(tmp_path):
    created = client.post("/tasks", json={
        "title": "Session inventory", "prompt": "Inspect", "workspace_path": str(tmp_path),
    })
    assert created.status_code == 201
    task_id = created.json()["id"]
    session_id = "session-phase-6-6-test"
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        task.status = "running"
        task.started_at = utcnow()
        task.session_id = session_id
        db.add(TaskEvent(
            task_id=task_id,
            event_type="agent.session.started",
            payload={"session_id": session_id, "worker_id": "worker-test", "attempt": 1},
        ))
        db.commit()

    listed = client.get("/sessions")
    assert listed.status_code == 200
    session = next(item for item in listed.json() if item["session_id"] == session_id)
    assert session["task_id"] == task_id
    assert session["controls"] == {"cancel": True, "pause": False, "resume": False}

    detail = client.get(f"/sessions/{session_id}")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["task_id"] == task_id
    assert any(event["event_type"] == "agent.session.started" for event in payload["events"])
    assert any("Pause/resume are not supported" in item for item in payload["limitations"])


def test_running_task_cancel_records_request_surface(tmp_path):
    created = client.post("/tasks", json={
        "title": "Audited running cancel", "prompt": "Inspect", "workspace_path": str(tmp_path),
    })
    task_id = created.json()["id"]
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        task.status = "running"
        task.started_at = utcnow()
        task.worker_id = "worker-test"
        db.commit()

    response = client.post(f"/tasks/{task_id}/cancel")
    assert response.status_code == 200
    events = client.get(f"/tasks/{task_id}/events").json()
    requested = [event for event in events if event["event_type"] == "task.cancel_requested"]
    assert len(requested) == 1
    assert requested[0]["payload"]["action"] == "cancel"
    assert requested[0]["payload"]["requested_by"] == "api_client"
    assert requested[0]["payload"]["control_surface"] == "task_api"
