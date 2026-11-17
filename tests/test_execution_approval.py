from uuid import uuid4
from pathlib import Path
import subprocess
import asyncio
from types import SimpleNamespace
import pytest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fastapi.testclient import TestClient
from fastapi import HTTPException
from starlette.requests import Request

from ai_work_os.api import app
from ai_work_os.db import Base, SessionLocal
from ai_work_os import worker
from ai_work_os.execution_approval import approval_is_valid, require_local_control_plane
from ai_work_os.models import ExecutionApproval, Project, Task, TaskEvent, utcnow

client = TestClient(app)


def _configured_task(tmp_path):
    workspace = tmp_path / f"workspace-{uuid4().hex}"
    workspace.mkdir()
    project = client.post("/projects", json={
        "name": "审批测试项目", "slug": f"approval-{uuid4().hex}",
        "workspace_path": str(workspace),
    })
    assert project.status_code == 201, project.text
    task = client.post("/tasks", json={
        "project_id": project.json()["id"],
        "title": "受控写入测试", "prompt": "在项目中创建一个测试文件",
        "workspace_path": str(workspace), "permission_mode": "configured",
    })
    assert task.status_code == 201, task.text
    return project.json(), task.json()


def test_configured_task_is_not_executable_without_approval(tmp_path):
    _, task = _configured_task(tmp_path)
    with SessionLocal() as db:
        record = db.get(Task, task["id"])
        assert record.permission_mode == "configured"
        assert bool(record.requires_approval) is False
        assert approval_is_valid(db, record) is False


def test_execution_approval_requires_explicit_decision_and_is_scope_bound(tmp_path):
    _, task = _configured_task(tmp_path)
    requested = client.post(f"/execution-approvals/request/{task['id']}")
    assert requested.status_code == 200, requested.text
    approval = requested.json()
    assert approval["status"] == "pending"
    assert approval["scope_valid"] is True
    assert approval["prompt"] == "在项目中创建一个测试文件"
    assert approval["verification_commands"] == []
    assert approval["max_repair_attempts"] == 0

    with SessionLocal() as db:
        record = db.get(Task, task["id"])
        assert approval_is_valid(db, record) is False

    approved = client.post(f"/execution-approvals/{approval['id']}/approve", json={"note": "确认本次变更范围"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    with SessionLocal() as db:
        record = db.get(Task, task["id"])
        assert approval_is_valid(db, record) is True
        record.prompt = "修改后的任务范围"
        db.commit()
        db.refresh(record)
        assert approval_is_valid(db, record) is False

    retry = client.post(f"/execution-approvals/{approval['id']}/approve", json={})
    assert retry.status_code == 409


def test_rejected_approval_never_becomes_valid(tmp_path):
    _, task = _configured_task(tmp_path)
    approval = client.post(f"/execution-approvals/request/{task['id']}").json()
    rejected = client.post(f"/execution-approvals/{approval['id']}/reject", json={"note": "暂不允许"})
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    with SessionLocal() as db:
        record = db.get(Task, task["id"])
        assert approval_is_valid(db, record) is False


def test_approval_is_invalidated_when_project_workspace_changes(tmp_path):
    project, task = _configured_task(tmp_path)
    approval = client.post(f"/execution-approvals/request/{task['id']}").json()
    approved = client.post(f"/execution-approvals/{approval['id']}/approve", json={})
    assert approved.status_code == 200
    updated = client.patch(f"/projects/{project['id']}", json={"workspace_path": str(tmp_path)})
    assert updated.status_code == 200
    with SessionLocal() as db:
        record = db.get(Task, task["id"])
        assert approval_is_valid(db, record) is False


def test_worker_never_claims_unapproved_configured_task_or_starts_after_revocation(monkeypatch, tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(worker, "SessionLocal", factory)
    try:
        with factory() as db:
            project = Project(name="隔离测试", slug="isolated-approval", workspace_path=str(tmp_path), status="active")
            db.add(project)
            db.flush()
            task = Task(
                project_id=project.id, title="受控任务", prompt="创建文件",
                workspace_path=str(tmp_path), permission_mode="configured",
                requires_approval=True, status="queued",
            )
            db.add(task)
            db.flush()
            approval = ExecutionApproval(
                task_id=task.id, project_id=project.id, workspace_path=str(tmp_path),
                scope_hash=__import__("ai_work_os.execution_approval", fromlist=["task_scope_hash"]).task_scope_hash(task),
                status="pending", expires_at=utcnow() + __import__("datetime").timedelta(hours=1),
            )
            db.add(approval)
            db.commit()
            task_id, approval_id, project_id = task.id, approval.id, project.id

        assert worker.claim_task() is None
        with factory() as db:
            db.get(ExecutionApproval, approval_id).status = "approved"
            db.commit()
        assert worker.claim_task() == task_id

        with factory() as db:
            db.get(ExecutionApproval, approval_id).status = "revoked"
            db.commit()

        async def forbidden(*args, **kwargs):
            raise AssertionError("Claude subprocess setup must not run for a revoked approval")
        monkeypatch.setattr(worker, "write_isolated_mcp_config", forbidden)
        asyncio.run(worker._execute_task(task_id, "legacy-worker"))
        with factory() as db:
            record = db.get(Task, task_id)
            assert record.status == "failed"
            events = list(db.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id)))
            invalid = [event for event in events if event.event_type == "execution.approval_invalid"]
            assert len(invalid) == 1
            assert invalid[0].payload["process_started"] is False
    finally:
        engine.dispose()


def test_expired_approval_is_not_listed_as_pending_and_cannot_be_approved(tmp_path):
    _, task = _configured_task(tmp_path)
    approval = client.post(f"/execution-approvals/request/{task['id']}").json()
    with SessionLocal() as db:
        record = db.get(ExecutionApproval, approval["id"])
        record.expires_at = utcnow() - __import__("datetime").timedelta(seconds=1)
        db.commit()
    pending = client.get("/execution-approvals?status=pending")
    assert pending.status_code == 200
    assert all(item["id"] != approval["id"] for item in pending.json()["approvals"])
    rejected = client.post(f"/execution-approvals/{approval['id']}/approve", json={})
    assert rejected.status_code == 409
    with SessionLocal() as db:
        record = db.get(ExecutionApproval, approval["id"])
        assert record.status == "expired"


def test_approved_queued_task_can_have_approval_revoked(tmp_path):
    _, task = _configured_task(tmp_path)
    approval = client.post(f"/execution-approvals/request/{task['id']}").json()
    approved = client.post(f"/execution-approvals/{approval['id']}/approve", json={})
    assert approved.status_code == 200
    revoked = client.post(f"/execution-approvals/{approval['id']}/revoke", json={"note": "撤回本次授权"})
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    with SessionLocal() as db:
        record = db.get(Task, task["id"])
        assert approval_is_valid(db, record) is False


def test_archived_project_invalidates_pending_write_execution(tmp_path):
    project, task = _configured_task(tmp_path)
    approval = client.post(f"/execution-approvals/request/{task['id']}").json()
    approved = client.post(f"/execution-approvals/{approval['id']}/approve", json={})
    assert approved.status_code == 200
    with SessionLocal() as db:
        db.get(Project, project["id"]).status = "archived"
        db.commit()
        record = db.get(Task, task["id"])
        assert approval_is_valid(db, record) is False
    archived_approval = client.get(f"/execution-approvals/{approval['id']}").json()
    assert archived_approval["can_resubmit"] is False


def test_local_start_script_waits_for_api_health_before_workers():
    script = Path(__file__).resolve().parents[1] / "scripts" / "start-local.sh"
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    source = script.read_text(encoding="utf-8")
    assert source.index("uvicorn ai_work_os.api:app") < source.index('curl --silent')
    assert source.index('curl --silent') < source.index("python -m ai_work_os.worker")
    assert source.index("python -m ai_work_os.worker") < source.index("python -m ai_work_os.master_agent")


def test_cancelling_queued_task_invalidates_pending_approval(tmp_path):
    _, task = _configured_task(tmp_path)
    approval = client.post(f"/execution-approvals/request/{task['id']}").json()
    cancelled = client.post(f"/tasks/{task['id']}/cancel")
    assert cancelled.status_code == 200
    pending = client.get("/execution-approvals?status=pending")
    assert all(item["id"] != approval["id"] for item in pending.json()["approvals"])
    with SessionLocal() as db:
        record = db.get(ExecutionApproval, approval["id"])
        assert record.status == "revoked"
        assert record.decision_source == "task_state"


@pytest.mark.parametrize("state_change, expected_status", [
    ("revoke", "failed"),
    ("project_workspace", "failed"),
    ("cancel", "cancelled"),
])
def test_worker_rechecks_approval_and_cancellation_immediately_before_spawn(
    monkeypatch, tmp_path, state_change, expected_status
):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(worker, "SessionLocal", factory)
    monkeypatch.setattr(worker, "STORAGE", tmp_path / "storage")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    try:
        with factory() as db:
            project = Project(
                name="启动竞态测试", slug=f"spawn-{uuid4().hex}",
                workspace_path=str(workspace), status="active",
            )
            db.add(project)
            db.flush()
            task = Task(
                project_id=project.id, title="启动前门禁", prompt="创建一个文件",
                workspace_path=str(workspace), permission_mode="configured",
                requires_approval=True, status="running", worker_id="race-worker",
                attempt_count=1, cancel_requested=0,
                lease_expires_at=utcnow() + __import__("datetime").timedelta(minutes=2),
            )
            db.add(task)
            db.flush()
            approval = ExecutionApproval(
                task_id=task.id, project_id=project.id, workspace_path=str(workspace),
                scope_hash=__import__(
                    "ai_work_os.execution_approval", fromlist=["task_scope_hash"]
                ).task_scope_hash(task),
                status="approved",
                expires_at=utcnow() + __import__("datetime").timedelta(hours=1),
            )
            db.add(approval)
            db.commit()
            task_id, approval_id, project_id = task.id, approval.id, project.id

        async def snapshot(*args, **kwargs):
            return {"files": []}

        monkeypatch.setattr(worker, "capture_workspace_snapshot", snapshot)
        monkeypatch.setattr(worker, "claude_setting_sources", lambda: ["project", "local"])

        def prepare_config(*args, **kwargs):
            # Simulate a state change after early validation but before process spawn.
            with factory() as db:
                if state_change == "revoke":
                    db.get(ExecutionApproval, approval_id).status = "revoked"
                elif state_change == "project_workspace":
                    other_workspace = tmp_path / "other-workspace"
                    other_workspace.mkdir(exist_ok=True)
                    db.get(Project, project_id).workspace_path = str(other_workspace)
                else:
                    db.get(Task, task_id).cancel_requested = 1
                db.commit()
            return tmp_path / "mcp.json"

        monkeypatch.setattr(worker, "write_isolated_mcp_config", prepare_config)
        monkeypatch.setattr(
            worker, "build_command",
            lambda *args, **kwargs: ["fake-claude", "-p", "测试提示词", "--mcp-config", "fake-config"],
        )
        monkeypatch.setattr(
            worker, "build_task_sandbox_invocation",
            lambda *args, **kwargs: SimpleNamespace(
                command=("fake-claude",), cwd=str(workspace), env={}
            ),
        )

        async def forbidden_spawn(*args, **kwargs):
            raise AssertionError("Claude child process must not start after approval/cancel changes")

        monkeypatch.setattr(worker.asyncio, "create_subprocess_exec", forbidden_spawn)
        asyncio.run(worker._execute_task(task_id, "race-worker"))

        with factory() as db:
            record = db.get(Task, task_id)
            assert record.status == expected_status
            assert record.worker_id is None
            events = list(db.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id)))
            assert not any(event.event_type == "executor.started" for event in events)
            if state_change in {"revoke", "project_workspace"}:
                blocked = [event for event in events if event.event_type == "execution.approval_invalid"]
                assert len(blocked) == 1, f"events={[(event.event_type, event.payload) for event in events]!r}; error={record.error_summary!r}"
                assert blocked[0].payload["process_started"] is False
                assert blocked[0].payload["check"] == "immediately_before_spawn"
            else:
                cancelled = [event for event in events if event.event_type == "task.cancelled"]
                assert len(cancelled) == 1, f"events={[(event.event_type, event.payload) for event in events]!r}; error={record.error_summary!r}"
                assert cancelled[0].payload["process_started"] is False
    finally:
        engine.dispose()


def test_repair_runner_rechecks_approval_before_repair_process_spawn(monkeypatch, tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(worker, "SessionLocal", factory)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_dir = tmp_path / "task-artifacts"
    task_dir.mkdir()

    try:
        with factory() as db:
            project = Project(
                name="修复审批测试", slug=f"repair-{uuid4().hex}",
                workspace_path=str(workspace), status="active",
            )
            db.add(project)
            db.flush()
            task = Task(
                project_id=project.id, title="修复门禁", prompt="修复验证失败",
                workspace_path=str(workspace), permission_mode="configured",
                requires_approval=True, status="running", worker_id="repair-worker",
                attempt_count=1,
                lease_expires_at=utcnow() + __import__("datetime").timedelta(minutes=2),
            )
            db.add(task)
            db.flush()
            approval = ExecutionApproval(
                task_id=task.id, project_id=project.id, workspace_path=str(workspace),
                scope_hash=__import__(
                    "ai_work_os.execution_approval", fromlist=["task_scope_hash"]
                ).task_scope_hash(task),
                status="approved",
                expires_at=utcnow() + __import__("datetime").timedelta(hours=1),
            )
            db.add(approval)
            db.commit()
            task_id, approval_id = task.id, approval.id

        monkeypatch.setattr(worker, "claude_setting_sources", lambda: ["project", "local"])

        def prepare_config(*args, **kwargs):
            with factory() as db:
                db.get(ExecutionApproval, approval_id).status = "revoked"
                db.commit()
            return task_dir / "mcp.json"

        monkeypatch.setattr(worker, "write_isolated_mcp_config", prepare_config)
        monkeypatch.setattr(worker, "build_command", lambda *args, **kwargs: ["fake-claude"])
        monkeypatch.setattr(
            worker, "build_task_sandbox_invocation",
            lambda *args, **kwargs: SimpleNamespace(
                command=("fake-claude",), cwd=str(workspace), env={}
            ),
        )

        async def forbidden_spawn(*args, **kwargs):
            raise AssertionError("repair Claude process must not start after approval revocation")

        monkeypatch.setattr(worker.asyncio, "create_subprocess_exec", forbidden_spawn)
        passed, results, error = asyncio.run(worker.run_repair_attempt(
            task_id, workspace, "configured", "修复验证", task_dir, 1, 1, [],
        ))
        assert passed is False
        assert results == []
        assert error == "approval_invalid_or_lease_lost"

        with factory() as db:
            record = db.get(Task, task_id)
            assert record.status == "failed"
            events = list(db.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id)))
            assert any(event.event_type == "execution.approval_invalid" for event in events)
            assert not any(event.event_type == "repair.started" for event in events)
    finally:
        engine.dispose()


def test_local_approval_guard_rejects_dns_rebinding_host_and_origin():
    request = Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/execution-approvals/123/approve",
        "raw_path": b"/execution-approvals/123/approve",
        "query_string": b"",
        "headers": [
            (b"host", b"attacker.example"),
            (b"origin", b"http://attacker.example"),
        ],
        "client": ("127.0.0.1", 43122),
        "server": ("127.0.0.1", 8000),
    })
    with pytest.raises(HTTPException) as exc:
        require_local_control_plane(request)
    assert exc.value.status_code == 403


def test_local_approval_guard_accepts_loopback_host_and_origin():
    request = Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/execution-approvals/123/approve",
        "raw_path": b"/execution-approvals/123/approve",
        "query_string": b"",
        "headers": [
            (b"host", b"127.0.0.1:8000"),
            (b"origin", b"http://127.0.0.1:8000"),
        ],
        "client": ("127.0.0.1", 43122),
        "server": ("127.0.0.1", 8000),
    })
    require_local_control_plane(request)


def test_runtime_policy_change_invalidates_and_allows_resubmission(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "off")
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_NETWORK", "none")
    _, task = _configured_task(tmp_path)
    requested = client.post(f"/execution-approvals/request/{task['id']}")
    assert requested.status_code == 200, requested.text
    approval = requested.json()
    assert approval["runtime_policy"]["sandbox_mode"] == "off"

    approved = client.post(f"/execution-approvals/{approval['id']}/approve", json={})
    assert approved.status_code == 200, approved.text
    assert approved.json()["scope_valid"] is True

    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "bubblewrap")
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_NETWORK", "host")
    stale = client.get(f"/execution-approvals/{approval['id']}").json()
    assert stale["scope_valid"] is False

    resubmitted = client.post(f"/execution-approvals/request/{task['id']}")
    assert resubmitted.status_code == 200, resubmitted.text
    current = resubmitted.json()
    assert current["id"] == approval["id"]
    assert current["status"] == "pending"
    assert current["scope_valid"] is True
    assert current["runtime_policy"]["sandbox_mode"] == "bubblewrap"
    assert current["runtime_policy"]["sandbox_network"] == "host"

    events = client.get(f"/tasks/{task['id']}/events").json()
    requested_events = [event for event in events if event["event_type"] == "execution.approval_requested"]
    assert len(requested_events) == 2
    with SessionLocal() as db:
        record = db.get(Task, task["id"])
        assert approval_is_valid(db, record) is False



def test_claude_settings_changes_invalidate_approval_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_WORK_OS_CLAUDE_SETTING_SOURCES", "project,local")
    project, task = _configured_task(tmp_path)
    settings_dir = Path(project["workspace_path"]) / ".claude"
    settings_dir.mkdir()
    settings_file = settings_dir / "settings.local.json"
    settings_file.write_text('{"permissions":{"allow":["Read"]}}', encoding="utf-8")

    requested = client.post(f"/execution-approvals/request/{task['id']}")
    assert requested.status_code == 200, requested.text
    approval = requested.json()
    approved = client.post(f"/execution-approvals/{approval['id']}/approve", json={})
    assert approved.status_code == 200, approved.text
    with SessionLocal() as db:
        record = db.get(Task, task["id"])
        assert approval_is_valid(db, record) is True

    settings_file.write_text('{"hooks":{"PreToolUse":[]}}', encoding="utf-8")
    with SessionLocal() as db:
        record = db.get(Task, task["id"])
        assert approval_is_valid(db, record) is False

    stale = client.get(f"/execution-approvals/{approval['id']}").json()
    assert stale["scope_valid"] is False
    assert stale["can_resubmit"] is True



def test_revoked_approval_is_not_claimed_again(tmp_path):
    _, task = _configured_task(tmp_path)
    approval = client.post(f"/execution-approvals/request/{task['id']}").json()
    approved = client.post(f"/execution-approvals/{approval['id']}/approve", json={})
    assert approved.status_code == 200
    with SessionLocal() as db:
        record = db.get(Task, task["id"])
        record.status = "queued"
        record.worker_id = None
        record.lease_expires_at = None
        db.commit()
    revoked = client.post(f"/execution-approvals/{approval['id']}/revoke", json={})
    assert revoked.status_code == 200
    claimed_task_id = worker.claim_task()
    assert claimed_task_id != task["id"]
    with SessionLocal() as db:
        record = db.get(Task, task["id"])
        assert record.status == "queued"
        assert record.worker_id is None
