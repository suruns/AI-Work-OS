import asyncio
from fastapi.testclient import TestClient
from ai_work_os.api import app
from ai_work_os import worker

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_dashboard_is_served():
    response = client.get("/")
    assert response.status_code == 200
    assert "AI Work OS" in response.text


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
