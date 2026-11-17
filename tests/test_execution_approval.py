from uuid import uuid4
from pathlib import Path
import subprocess
import asyncio

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fastapi.testclient import TestClient

from ai_work_os.api import app
from ai_work_os.db import Base, SessionLocal
from ai_work_os import worker
from ai_work_os.execution_approval import approval_is_valid
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
            task_id, approval_id = task.id, approval.id

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


def test_local_start_script_waits_for_api_health_before_workers():
    script = Path(__file__).resolve().parents[1] / "scripts" / "start-local.sh"
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    source = script.read_text(encoding="utf-8")
    assert source.index("uvicorn ai_work_os.api:app") < source.index('curl --silent')
    assert source.index('curl --silent') < source.index("python -m ai_work_os.worker")
    assert source.index("python -m ai_work_os.worker") < source.index("python -m ai_work_os.master_agent")
