from uuid import uuid4

from fastapi.testclient import TestClient

from ai_work_os.api import app
from ai_work_os.db import SessionLocal
from ai_work_os.execution_approval import approval_is_valid
from ai_work_os.models import Task

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
