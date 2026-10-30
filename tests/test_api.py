import asyncio
import json
import subprocess
import sys
from datetime import timedelta
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
    assert "function renderArtifacts(data)" in response.text
    assert "Workspace files changed during task" in response.text


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
        task.started_at = utcnow()
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
    assert worker.claim_task() == task_ids["prepare"]
    assert worker.claim_task() is None

    with SessionLocal() as db:
        first = db.get(Task, task_ids["prepare"])
        first.status = "succeeded"
        first.finished_at = utcnow()
        db.commit()

    assert worker.claim_task() == task_ids["build"]


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

    assert worker.claim_task() == ids["first"]
    assert worker.claim_task() == ids["second"]
    assert worker.claim_task() is None

    with SessionLocal() as db:
        first = db.get(Task, ids["first"])
        second = db.get(Task, ids["second"])
        first.status = "succeeded"
        first.finished_at = utcnow()
        # Simulate the second parent waiting for its retry backoff.
        second.status = "queued"
        second.available_at = utcnow() + timedelta(hours=1)
        db.commit()

    assert worker.claim_task() is None
    with SessionLocal() as db:
        second = db.get(Task, ids["second"])
        second.status = "succeeded"
        second.finished_at = utcnow()
        db.commit()

    assert worker.claim_task() == ids["join"]
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

    worker.reconcile_workflows()
    assert client.get(f"/workflows/{workflow_id}").json()["status"] == "failed"
    with SessionLocal() as db:
        parent = db.get(Task, ids["slow-parent"])
        parent.status = "failed"
        parent.finished_at = utcnow()
        db.commit()

    worker.reconcile_workflows()
    detail = client.get(f"/workflows/{workflow_id}").json()
    states = {task["id"]: task["status"] for task in detail["tasks"]}
    assert states[ids["slow-child"]] == "blocked"



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
    assert worker.claim_task() != task_id
    with SessionLocal() as db:
        assert db.get(Task, task_id).status == "queued"
