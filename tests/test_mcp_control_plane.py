from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ai_work_os import mcp_control_plane as mcp
from ai_work_os.db import Base
from ai_work_os.models import Project, Task, TaskEvent, Workflow


def isolated(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(mcp, "SessionLocal", factory)
    project = Project(
        name="Scoped project", slug="scoped-project",
        workspace_path=str(tmp_path), status="active",
    )
    other = Project(
        name="Other project", slug="other-project",
        workspace_path=str(tmp_path), status="active",
    )
    with factory() as db:
        db.add_all([project, other])
        db.commit()
        db.refresh(project)
        db.refresh(other)
        ids = (project.id, other.id)
    monkeypatch.setattr(mcp, "PROJECT_ID", ids[0])
    return engine, factory, ids


def test_tool_schemas_do_not_accept_project_or_workspace_authority():
    tools = {item["name"]: item["inputSchema"] for item in mcp._tools()}
    for schema in tools.values():
        assert schema["additionalProperties"] is False
        assert "project_id" not in schema["properties"]
        assert "workspace_path" not in schema["properties"]
    assert "create_task" in tools
    assert "create_workflow" in tools


def test_task_creation_is_project_bound_plan_only_and_only_queues(monkeypatch, tmp_path):
    engine, factory, (project_id, _) = isolated(monkeypatch, tmp_path)
    try:
        result = mcp.call_tool("create_task", {
            "title": "Inspect API", "prompt": "Review API behavior",
        })
        with factory() as db:
            task = db.get(Task, result["id"])
            assert task.project_id == project_id
            assert task.workspace_path == str(tmp_path)
            assert task.permission_mode == "plan"
            assert task.status == "queued"
            assert not task.auto_repair
            events = list(db.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id)))
            assert [event.event_type for event in events] == ["task.queued"]
    finally:
        engine.dispose()


def test_cross_project_task_and_workflow_ids_are_not_disclosed(monkeypatch, tmp_path):
    engine, factory, (project_id, other_id) = isolated(monkeypatch, tmp_path)
    try:
        with factory() as db:
            task = Task(project_id=other_id, title="Secret", prompt="secret",
                        workspace_path=str(tmp_path), permission_mode="plan")
            workflow = Workflow(project_id=other_id, title="Secret workflow")
            db.add_all([task, workflow])
            db.commit()
            task_id, workflow_id = task.id, workflow.id
        for name, args in (
            ("get_task", {"task_id": task_id}),
            ("get_workflow", {"workflow_id": workflow_id}),
        ):
            try:
                mcp.call_tool(name, args)
            except mcp.ToolError as exc:
                assert "not found" in str(exc)
            else:
                raise AssertionError("cross-project object was exposed")
        overview = mcp.call_tool("get_project_overview", {})
        assert overview["id"] == project_id
    finally:
        engine.dispose()


def test_workflow_creation_is_atomic_and_validates_dag(monkeypatch, tmp_path):
    engine, factory, (project_id, _) = isolated(monkeypatch, tmp_path)
    try:
        result = mcp.call_tool("create_workflow", {
            "title": "Build and test",
            "tasks": [
                {"key": "build", "title": "Build", "prompt": "Build the app"},
                {"key": "test", "title": "Test", "prompt": "Test the app", "depends_on": ["build"]},
            ],
        })
        assert result["counts"] == {"queued": 2}
        assert len(result["tasks"]) == 2
        assert len(result["dependencies"]) == 1
        with factory() as db:
            tasks = list(db.scalars(select(Task).where(Task.project_id == project_id)))
            assert len(tasks) == 2
            assert all(task.permission_mode == "plan" for task in tasks)
            assert all(task.status == "queued" for task in tasks)
            assert len(list(db.scalars(select(TaskEvent)))) == 2
        try:
            mcp.call_tool("create_workflow", {
                "title": "Cycle",
                "tasks": [
                    {"key": "a", "title": "A", "prompt": "A", "depends_on": ["b"]},
                    {"key": "b", "title": "B", "prompt": "B", "depends_on": ["a"]},
                ],
            })
        except mcp.ToolError as exc:
            assert "acyclic" in str(exc)
        else:
            raise AssertionError("cyclic workflow was accepted")
        with factory() as db:
            assert db.scalar(select(Workflow).where(Workflow.title == "Cycle")) is None
    finally:
        engine.dispose()


def test_mcp_json_rpc_initialize_and_tool_listing():
    init = mcp._reply({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init["result"]["capabilities"]["tools"] == {}
    listed = mcp._reply({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert names == {
        "get_project_overview", "list_tasks", "get_task", "list_workflows",
        "get_workflow", "create_task", "create_workflow",
    }
