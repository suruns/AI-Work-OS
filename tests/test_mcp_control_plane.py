import os
import subprocess
import sys

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ai_work_os import mcp_control_plane as mcp
from ai_work_os.db import Base
from ai_work_os.models import (AgentTurn, Conversation, ConversationMessage, ExecutionApproval, MCPToolInvocation, Project, Task, TaskEvent, Workflow)


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
    conversation = Conversation(project_id=ids[0], title="MCP idempotency", status="active")
    with factory() as db:
        db.add(conversation)
        db.flush()
        message = ConversationMessage(
            conversation_id=conversation.id, role="user", content="Create a task",
            message_metadata={},
        )
        db.add(message)
        db.flush()
        turn = AgentTurn(
            conversation_id=conversation.id, user_message_id=message.id,
            status="running", adapter="claude-code",
        )
        db.add(turn)
        db.commit()
        turn_id = turn.id
    monkeypatch.setattr(mcp, "TURN_ID", turn_id)
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
                assert "不存在" in str(exc)
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
        "get_workflow", "create_task", "create_workflow", "request_execution",
    }


def test_create_task_json_rpc_retry_returns_original_result(monkeypatch, tmp_path):
    engine, factory, (project_id, _) = isolated(monkeypatch, tmp_path)
    try:
        request = {
            "jsonrpc": "2.0", "id": "task-call-1", "method": "tools/call",
            "params": {"name": "create_task", "arguments": {
                "title": "Exactly once", "prompt": "Create this task once",
            }},
        }
        first = mcp._reply(request)
        retry = mcp._reply(request)
        first_value = __import__("json").loads(first["result"]["content"][0]["text"])
        retry_value = __import__("json").loads(retry["result"]["content"][0]["text"])
        assert first_value["id"] == retry_value["id"]
        with factory() as db:
            assert db.scalar(select(Task.id).where(Task.project_id == project_id)) == first_value["id"]
            assert len(list(db.scalars(select(Task).where(Task.project_id == project_id)))) == 1
            records = list(db.scalars(select(MCPToolInvocation)))
            assert len(records) == 1
            assert records[0].request_id == "s:task-call-1"
    finally:
        engine.dispose()


def test_create_workflow_json_rpc_retry_is_atomic_and_idempotent(monkeypatch, tmp_path):
    engine, factory, (project_id, _) = isolated(monkeypatch, tmp_path)
    try:
        request = {
            "jsonrpc": "2.0", "id": 52, "method": "tools/call",
            "params": {"name": "create_workflow", "arguments": {
                "title": "One workflow",
                "tasks": [
                    {"key": "a", "title": "A", "prompt": "A"},
                    {"key": "b", "title": "B", "prompt": "B", "depends_on": ["a"]},
                ],
            }},
        }
        first = mcp._reply(request)
        retry = mcp._reply(request)
        first_value = __import__("json").loads(first["result"]["content"][0]["text"])
        retry_value = __import__("json").loads(retry["result"]["content"][0]["text"])
        assert first_value["id"] == retry_value["id"]
        assert [task["id"] for task in first_value["tasks"]] == [
            task["id"] for task in retry_value["tasks"]
        ]
        with factory() as db:
            assert len(list(db.scalars(select(Workflow).where(Workflow.project_id == project_id)))) == 1
            assert len(list(db.scalars(select(Task).where(Task.project_id == project_id)))) == 2
            assert len(list(db.scalars(select(MCPToolInvocation)))) == 1
    finally:
        engine.dispose()


def test_json_rpc_request_id_cannot_be_reused_for_another_tool(monkeypatch, tmp_path):
    engine, factory, _ = isolated(monkeypatch, tmp_path)
    try:
        created = mcp._reply({
            "jsonrpc": "2.0", "id": "shared-id", "method": "tools/call",
            "params": {"name": "create_task", "arguments": {
                "title": "A", "prompt": "A",
            }},
        })
        assert created["result"]["isError"] is False
        conflict = mcp._reply({
            "jsonrpc": "2.0", "id": "shared-id", "method": "tools/call",
            "params": {"name": "create_workflow", "arguments": {
                "title": "B", "tasks": [{"key": "b", "title": "B", "prompt": "B"}],
            }},
        })
        assert conflict["result"]["isError"] is True
        assert "different tool arguments" in conflict["result"]["content"][0]["text"]
        with factory() as db:
            assert len(list(db.scalars(select(Task)))) == 1
            assert len(list(db.scalars(select(Workflow)))) == 0
    finally:
        engine.dispose()

 
def test_json_rpc_request_id_reuse_with_different_arguments_is_rejected(monkeypatch, tmp_path):
    engine, factory, _ = isolated(monkeypatch, tmp_path)
    try:
        first = mcp._reply({
            "jsonrpc": "2.0", "id": "same-id", "method": "tools/call",
            "params": {"name": "create_task", "arguments": {
                "title": "Original", "prompt": "Original arguments",
            }},
        })
        assert first["result"]["isError"] is False
        retry = mcp._reply({
            "jsonrpc": "2.0", "id": "same-id", "method": "tools/call",
            "params": {"name": "create_task", "arguments": {
                "title": "Different", "prompt": "Different arguments",
            }},
        })
        assert retry["result"]["isError"] is True
        assert "different tool arguments" in retry["result"]["content"][0]["text"]
        with factory() as db:
            assert len(list(db.scalars(select(Task)))) == 1
    finally:
        engine.dispose()


def test_broker_client_import_does_not_initialize_host_database(tmp_path):
    db_path = tmp_path / "host-storage" / "awos.db"
    env = os.environ.copy()
    env["AI_WORK_OS_MCP_BROKER_SOCKET"] = str(tmp_path / "missing-broker.sock")
    env["AI_WORK_OS_DB"] = str(db_path)
    code = (
        "import sys; import ai_work_os.mcp_control_plane; "
        "assert 'ai_work_os.db' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True, env=env)
    assert not db_path.parent.exists()


def test_request_execution_creates_pending_approval_and_is_idempotent(monkeypatch, tmp_path):
    engine, factory, (project_id, _) = isolated(monkeypatch, tmp_path)
    try:
        args = {
            "title": "修复登录页面", "prompt": "修复项目中的登录页面并运行验证",
            "acceptance_criteria": "登录流程测试通过", "verification_commands": ["pytest -q tests/test_login.py"],
        }
        first = mcp.call_tool("request_execution", args, request_id="write-request-1")
        second = mcp.call_tool("request_execution", args, request_id="write-request-1")
        assert first == second
        assert first["permission_mode"] == "configured"
        assert first["requires_approval"] is True
        assert first["approval_status"] == "pending"
        with factory() as db:
            task = db.get(Task, first["id"])
            approval = db.scalar(select(ExecutionApproval).where(ExecutionApproval.task_id == task.id))
            assert task.project_id == project_id
            assert task.status == "queued"
            assert task.verification_commands == ["pytest -q tests/test_login.py"]
            assert approval.status == "pending"
            assert approval.project_id == project_id
            assert approval.scope_hash
            assert db.scalar(select(MCPToolInvocation).where(
                MCPToolInvocation.request_id == "s:write-request-1"
            )) is not None
    finally:
        engine.dispose()
