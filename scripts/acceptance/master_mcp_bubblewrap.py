from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path


def _walk(value):
    if isinstance(value, dict):
        if value.get("type") == "tool_use" and isinstance(value.get("name"), str):
            yield value["name"]
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "src"))
    claude_bin = os.getenv("AI_WORK_OS_CLAUDE_BIN", "claude")
    if shutil.which(claude_bin) is None:
        print("FAIL: Claude Code CLI is not available on PATH")
        return 2
    if not os.getenv("AI_WORK_OS_CLAUDE_MODEL", "").strip():
        print("FAIL: set AI_WORK_OS_CLAUDE_MODEL to a model available through this CLI gateway")
        return 2
    if os.getenv("AI_WORK_OS_SANDBOX_MODE", "").strip().lower() != "bubblewrap":
        print("FAIL: set AI_WORK_OS_SANDBOX_MODE=bubblewrap")
        return 2
    # The remote model gateway must be reachable from inside the sandbox.
    os.environ.setdefault("AI_WORK_OS_SANDBOX_NETWORK", "host")

    with tempfile.TemporaryDirectory(prefix="awos-real-bwrap-mcp-") as temp:
        temp_path = Path(temp)
        workspace = temp_path / "workspace"
        workspace.mkdir()
        storage = temp_path / "storage"
        storage.mkdir()
        db_path = temp_path / "awos.db"
        os.environ["AI_WORK_OS_DB"] = f"sqlite:///{db_path}"
        os.environ["AI_WORK_OS_STORAGE"] = str(storage)

        from sqlalchemy import select
        from ai_work_os.db import Base, SessionLocal, engine
        from ai_work_os.models import (
            AgentTurn, AgentTurnEvent, Conversation, ConversationMessage,
            Project, Task, Worker, Workflow, WorkflowTaskDependency,
        )
        from ai_work_os.master_agent import _claim_turn, execute_turn

        Base.metadata.create_all(bind=engine)
        with SessionLocal() as db:
            project = Project(
                name="Bubblewrap MCP Acceptance",
                slug="bubblewrap-mcp-acceptance",
                workspace_path=str(workspace),
                status="active",
            )
            conversation = Conversation(
                project_id=None, title="Bubblewrap MCP acceptance", status="active",
            )
            db.add_all([project, conversation])
            db.flush()
            conversation.project_id = project.id
            db.commit()
            project_id, conversation_id = project.id, conversation.id

        first_prompt = """Use the connected ai-work-os MCP tools now; do not merely describe actions.
Call get_project_overview and list_tasks first.
Then call create_task exactly once with title "Bubblewrap MCP task" and prompt
"Inspect the project structure and report; do not modify files."
Then call create_workflow exactly once with title "Bubblewrap MCP DAG" and three tasks:
- inspect: inspect the project structure
- implement: plan a minimal implementation, depends_on ["inspect"]
- verify: plan validation, depends_on ["implement"]
After all four tool calls succeed, briefly report the returned task and workflow IDs.
Do not use shell commands or file-editing tools."""
        second_prompt = (
            "This is a follow-up in the same conversation. "
            "Reply with exactly SESSION_RESUMED_OK and do not call tools."
        )

        def enqueue_turn(content: str) -> str:
            with SessionLocal() as db:
                message = ConversationMessage(
                    conversation_id=conversation_id, role="user", content=content,
                    message_metadata={},
                )
                db.add(message)
                db.flush()
                turn = AgentTurn(
                    conversation_id=conversation_id, user_message_id=message.id,
                    status="queued", adapter="claude-code",
                )
                db.add(turn)
                db.commit()
                return turn.id

        def run_turn(turn_id: str) -> None:
            worker_id = str(uuid.uuid4())
            claimed = _claim_turn(worker_id)
            if claimed != turn_id:
                raise RuntimeError(f"expected to claim {turn_id}, claimed {claimed}")
            asyncio.run(execute_turn(turn_id, worker_id))
            with SessionLocal() as db:
                turn = db.get(AgentTurn, turn_id)
                if turn is None or turn.status != "succeeded":
                    raise RuntimeError(
                        f"Master Agent turn failed: {turn.error_summary if turn else 'missing turn'}"
                    )

        try:
            first_turn_id = enqueue_turn(first_prompt)
            run_turn(first_turn_id)
            second_turn_id = enqueue_turn(second_prompt)
            run_turn(second_turn_id)
        except Exception as exc:
            print(f"FAIL: Bubblewrap Master Agent execution: {type(exc).__name__}: {exc}")
            return 1

        with SessionLocal() as db:
            project_tasks = list(db.scalars(select(Task).where(Task.project_id == project_id)))
            workflows = list(db.scalars(select(Workflow).where(Workflow.project_id == project_id)))
            edges = list(db.scalars(select(WorkflowTaskDependency)))
            workers = list(db.scalars(select(Worker)))
            conversation = db.get(Conversation, conversation_id)
            first_turn = db.get(AgentTurn, first_turn_id)
            second_turn = db.get(AgentTurn, second_turn_id)
            first_events = list(db.scalars(select(AgentTurnEvent).where(
                AgentTurnEvent.turn_id == first_turn_id
            )))
            second_events = list(db.scalars(select(AgentTurnEvent).where(
                AgentTurnEvent.turn_id == second_turn_id
            )))
            second_message = db.get(ConversationMessage, second_turn.assistant_message_id)
            second_text = second_message.content if second_message else ""
        called = set(name for event in first_events for name in _walk(event.payload))
        required = {
            "get_project_overview", "list_tasks", "create_task", "create_workflow",
        }
        missing = sorted(required - called)
        first_start = next((event for event in first_events if event.event_type == "turn.executor_started"), None)
        second_start = next((event for event in second_events if event.event_type == "turn.executor_started"), None)
        sandbox_meta = (first_start.payload or {}).get("sandbox", {}) if first_start else {}
        db_text = str(db_path)
        first_socket = Path("/tmp") / f"awos-mcp-{first_turn_id}.sock"
        second_socket = Path("/tmp") / f"awos-mcp-{second_turn_id}.sock"
        ok = (
            not missing
            and len(project_tasks) == 4
            and len(workflows) == 1
            and len(edges) == 2
            and len(workers) == 0
            and len(workflows) == 1 and workflows[0].status == "queued"
            and all(
                task.status == "queued" and task.permission_mode == "plan"
                and not task.auto_repair and task.attempt_count == 0
                for task in project_tasks
            )
            and conversation.cli_session_id
            and first_turn.cli_session_id == second_turn.cli_session_id == conversation.cli_session_id
            and "SESSION_RESUMED_OK" in second_text
            and sandbox_meta.get("backend") == "bubblewrap"
            and sandbox_meta.get("mcp_broker_socket") == "/run/awos-mcp.sock"
            and sandbox_meta.get("workspace_access") == "read_only"
            and bool(sandbox_meta.get("mcp_runtime_mounts"))
            and first_start is not None and second_start is not None
            and "--resume" not in first_start.payload.get("command", [])
            and "--resume" in second_start.payload.get("command", [])
            and db_text not in json.dumps(first_start.payload if first_start else {})
            and not first_socket.exists() and not second_socket.exists()
        )
        first_config_path = storage / "master-agent" / first_turn_id / "mcp-config.isolated.json"
        first_config = json.loads(first_config_path.read_text(encoding="utf-8"))
        broker_env = first_config["mcpServers"]["awos"]["env"]
        if broker_env.get("AI_WORK_OS_MCP_BROKER_SOCKET") != "/run/awos-mcp.sock" or "AI_WORK_OS_MCP_PROJECT_ID" in broker_env:
            ok = False
        if not ok:
            print("FAIL: Bubblewrap MCP acceptance assertions failed")
            print(f"missing_tools={missing} tasks={len(project_tasks)} workflows={len(workflows)} edges={len(edges)} workers={len(workers)}")
            print(f"session_ids={conversation.cli_session_id if conversation else None}, {first_turn.cli_session_id}, {second_turn.cli_session_id}")
            print(f"sandbox={sandbox_meta}")
            print(f"second_reply={second_text[-500:]}")
            return 1
        print("PASS: Bubblewrap project MCP broker, queued DAG, and resumed follow-up session")
        print("tools=" + ",".join(sorted(required)))
        print(f"tasks={len(project_tasks)} workflows={len(workflows)} dag_edges={len(edges)} workers={len(workers)}")
        print(f"session_id={conversation.cli_session_id}")
        print("broker_socket_cleanup=passed")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
