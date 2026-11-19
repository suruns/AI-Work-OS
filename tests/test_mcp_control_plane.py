import os
import subprocess
import sys

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ai_work_os import mcp_control_plane as mcp
from ai_work_os.db import Base
from ai_work_os.models import (AgentTurn, Conversation, ConversationMessage, ExecutionApproval, MCPToolInvocation, MasterRun, MasterRunTask, Project, Task, TaskEvent, Workflow)


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
            "verification_commands": ["python -m pytest -q tests/test_api.py"],
        })
        with factory() as db:
            task = db.get(Task, result["id"])
            assert task.project_id == project_id
            assert task.workspace_path == str(tmp_path)
            assert task.permission_mode == "plan"
            assert task.status == "queued"
            assert task.verification_commands == ["python -m pytest -q tests/test_api.py"]
            assert task.verification_status == "pending"
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
                {"key": "build", "title": "Build", "prompt": "Build the app", "verification_commands": ["python -m pytest -q"]},
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
            build_task = next(task for task in tasks if task.title == "Build")
            assert build_task.verification_commands == ["python -m pytest -q"]
            assert build_task.verification_status == "pending"
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
            assert "环" in str(exc)
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
    request_schema = next(
        tool["inputSchema"] for tool in listed["result"]["tools"]
        if tool["name"] == "request_execution"
    )
    assert "verification_commands" in request_schema["properties"]
    assert request_schema["properties"]["verification_commands"]["maxItems"] == 5
    assert all(any("\u4e00" <= char <= "\u9fff" for char in tool["description"]) for tool in listed["result"]["tools"])
    assert names == {
        "get_project_overview", "list_tasks", "get_task", "list_workflows",
        "get_workflow", "create_task", "create_workflow", "request_execution",
        "start_goal", "get_goal", "update_goal", "cancel_goal",
    }



def test_start_goal_is_persisted_and_idempotent(monkeypatch, tmp_path):
    engine, factory, (project_id, _) = isolated(monkeypatch, tmp_path)
    try:
        request = {
            "jsonrpc": "2.0", "id": "goal-start-1", "method": "tools/call",
            "params": {"name": "start_goal", "arguments": {
                "goal": "完成一个功能并验证",
                "max_steps": 12,
                "deadline_hours": 3,
            }},
        }
        first = mcp._reply(request)
        retry = mcp._reply(request)
        assert first["result"]["isError"] is False
        first_value = __import__("json").loads(first["result"]["content"][0]["text"])
        retry_value = __import__("json").loads(retry["result"]["content"][0]["text"])
        assert first_value["id"] == retry_value["id"]
        assert first_value["status"] == "planning"
        with factory() as db:
            runs = list(db.scalars(select(MasterRun)))
            assert len(runs) == 1
            assert runs[0].conversation_id
            assert runs[0].project_id == project_id
            assert runs[0].max_steps == 12
            assert len(list(db.scalars(select(MCPToolInvocation)))) == 1
    finally:
        engine.dispose()


def test_goal_checkpoint_update_is_idempotent_across_retries(monkeypatch, tmp_path):
    engine, factory, _ = isolated(monkeypatch, tmp_path)
    try:
        started = mcp.call_tool(
            "start_goal", {"goal": "跨轮次持续推进"},
            request_id="goal-create-once",
        )
        args = {
            "plan": {"steps": [{"id": "inspect", "status": "pending"}]},
            "checkpoint": {"last_completed_step": "none"},
            "increment_step": True,
        }
        first = mcp.call_tool("update_goal", args, request_id="goal-checkpoint-once")
        retry = mcp.call_tool("update_goal", args, request_id="goal-checkpoint-once")
        assert first["id"] == started["id"] == retry["id"]
        with factory() as db:
            run = db.get(MasterRun, started["id"])
            assert run.step_count == 1
            assert run.checkpoint == {"last_completed_step": "none"}
            assert run.plan["steps"][0]["id"] == "inspect"
            assert len(list(db.scalars(select(MCPToolInvocation)))) == 2
    finally:
        engine.dispose()


def test_tasks_created_during_active_goal_are_linked_automatically(monkeypatch, tmp_path):
    engine, factory, _ = isolated(monkeypatch, tmp_path)
    try:
        started = mcp._reply({
            "jsonrpc": "2.0", "id": "goal-start", "method": "tools/call",
            "params": {"name": "start_goal", "arguments": {"goal": "修复并验证"}},
        })
        assert started["result"]["isError"] is False
        created = mcp._reply({
            "jsonrpc": "2.0", "id": "goal-task", "method": "tools/call",
            "params": {"name": "create_task", "arguments": {
                "title": "检查代码", "prompt": "检查项目代码并给出发现",
            }},
        })
        assert created["result"]["isError"] is False
        task_value = __import__("json").loads(created["result"]["content"][0]["text"])
        with factory() as db:
            run = db.scalar(select(MasterRun))
            link = db.scalar(select(MasterRunTask).where(
                MasterRunTask.master_run_id == run.id,
                MasterRunTask.task_id == task_value["id"],
            ))
            assert link is not None and link.required
    finally:
        engine.dispose()


def test_cancel_goal_cancels_queued_tasks_and_is_idempotent(monkeypatch, tmp_path):
    engine, factory, _ = isolated(monkeypatch, tmp_path)
    try:
        started = mcp.call_tool(
            "start_goal", {"goal": "需要可取消的目标"},
            request_id="cancel-goal-start",
        )
        task = mcp.call_tool("create_task", {
            "title": "待执行任务", "prompt": "执行只读检查",
        }, request_id="cancel-goal-task")
        args = {"reason": "用户要求停止"}
        cancelled = mcp.call_tool("cancel_goal", args, request_id="cancel-goal-once")
        retry = mcp.call_tool("cancel_goal", args, request_id="cancel-goal-once")
        assert cancelled == retry
        assert cancelled["id"] == started["id"]
        assert cancelled["status"] == "cancelled"
        assert cancelled["cancelled_tasks"] == 1
        with factory() as db:
            run = db.get(MasterRun, started["id"])
            task_row = db.get(Task, task["id"])
            assert run.active_key is None
            assert task_row.status == "cancelled"
            assert task_row.cancel_requested == 1
            events = list(db.scalars(select(TaskEvent).where(TaskEvent.task_id == task["id"])))
            assert any(event.event_type == "task.cancelled" for event in events)
    finally:
        engine.dispose()


def test_goal_tools_require_request_ids(monkeypatch, tmp_path):
    engine, _, _ = isolated(monkeypatch, tmp_path)
    try:
        response = mcp._reply({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "start_goal", "arguments": {"goal": "No request id"}},
        })
        assert response["result"]["isError"] is True
        assert "request id" in response["result"]["content"][0]["text"]
    finally:
        engine.dispose()


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
        assert "不同的工具参数" in conflict["result"]["content"][0]["text"]
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
        assert "不同的工具参数" in retry["result"]["content"][0]["text"]
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
        mcp.call_tool("start_goal", {"goal": "修复登录页面并验证"}, request_id="goal-before-write")
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
            run = db.scalar(select(MasterRun))
            assert db.scalar(select(MasterRunTask.id).where(
                MasterRunTask.master_run_id == run.id,
                MasterRunTask.task_id == task.id,
            )) is not None
            assert db.scalar(select(MCPToolInvocation).where(
                MCPToolInvocation.request_id == "s:write-request-1"
            )) is not None
    finally:
        engine.dispose()


def test_cancel_goal_requests_cancellation_of_running_tasks(monkeypatch, tmp_path):
    engine, factory, _ = isolated(monkeypatch, tmp_path)
    try:
        mcp.call_tool("start_goal", {"goal": "取消运行中的任务"}, request_id="running-cancel-start")
        task = mcp.call_tool("create_task", {
            "title": "运行中任务", "prompt": "执行只读检查",
        }, request_id="running-cancel-task")
        with factory() as db:
            row = db.get(Task, task["id"])
            row.status = "running"
            db.commit()
        result = mcp.call_tool("cancel_goal", {}, request_id="running-cancel-goal")
        assert result["status"] == "cancelling"
        assert result["cancellation_requested_tasks"] == 1
        with factory() as db:
            row = db.get(Task, task["id"])
            assert row.status == "running"
            assert row.cancel_requested == 1
            events = list(db.scalars(select(TaskEvent).where(TaskEvent.task_id == task["id"])))
            assert any(event.event_type == "task.cancel_requested" for event in events)
    finally:
        engine.dispose()


def test_cancel_goal_revokes_pending_execution_approval(monkeypatch, tmp_path):
    engine, factory, _ = isolated(monkeypatch, tmp_path)
    try:
        mcp.call_tool("start_goal", {"goal": "取消待审批写入"}, request_id="approval-cancel-start")
        task = mcp.call_tool("request_execution", {
            "title": "受控写入申请", "prompt": "修改指定文件",
            "verification_commands": ["python -m pytest -q"],
        }, request_id="approval-cancel-task")
        assert task["approval_status"] == "pending"
        mcp.call_tool("cancel_goal", {}, request_id="approval-cancel-goal")
        with factory() as db:
            approval = db.scalar(select(ExecutionApproval).where(
                ExecutionApproval.task_id == task["id"],
            ))
            assert approval.status == "revoked"
            assert approval.decision_source == "task_state"
    finally:
        engine.dispose()


def test_workflow_and_dag_tasks_are_linked_to_active_goal(monkeypatch, tmp_path):
    engine, factory, _ = isolated(monkeypatch, tmp_path)
    try:
        started = mcp.call_tool(
            "start_goal", {"goal": "交付一个完整功能"},
            request_id="goal-dag-start",
        )
        workflow = mcp.call_tool("create_workflow", {
            "title": "功能交付 DAG",
            "tasks": [
                {"key": "inspect", "title": "检查", "prompt": "检查现状"},
                {"key": "build", "title": "实现", "prompt": "实现功能", "depends_on": ["inspect"]},
                {"key": "verify", "title": "验证", "prompt": "验证功能",
                 "depends_on": ["build"], "verification_commands": ["python -m pytest -q"]},
            ],
        }, request_id="goal-dag-create")
        with factory() as db:
            assert db.scalar(select(MasterRunWorkflow).where(
                MasterRunWorkflow.master_run_id == started["id"],
                MasterRunWorkflow.workflow_id == workflow["id"],
            )) is not None
            links = list(db.scalars(select(MasterRunTask).where(
                MasterRunTask.master_run_id == started["id"],
            )))
            assert len(links) == 3
            verify = db.get(Task, next(
                link.task_id for link in links
                if db.get(Task, link.task_id).title == "验证"
            ))
            assert verify.verification_commands == ["python -m pytest -q"]
    finally:
        engine.dispose()
