from fastapi.testclient import TestClient
from ai_work_os.api import app

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
    events = client.get(f"/tasks/{task['id']}/events")
    assert events.status_code == 200
    assert events.json()[0]["event_type"] == "task.queued"
