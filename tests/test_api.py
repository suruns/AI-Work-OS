from fastapi.testclient import TestClient
from ai_work_os.api import app


def test_health():
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_task_rejects_missing_workspace():
    response = TestClient(app).post("/tasks", json={"title": "test", "prompt": "read only", "workspace_path": "/path/that/should/not/exist/ai-work-os-test"})
    assert response.status_code == 422
