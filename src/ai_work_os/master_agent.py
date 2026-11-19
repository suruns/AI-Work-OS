from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import site
import socket
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import exists, select, update
from sqlalchemy.orm import aliased

from .db import Base, SessionLocal, engine, ensure_schema_compatibility
from .models import (
    AgentTurn, AgentTurnEvent, Conversation, ConversationMessage, ExecutionApproval,
    MasterRun, MasterRunTask, Project, Task, utcnow,
)
from .sandbox import build_sandbox_invocation, sandbox_mode

Base.metadata.create_all(bind=engine)
ensure_schema_compatibility()

CLAUDE_BIN = os.getenv("AI_WORK_OS_CLAUDE_BIN", "claude")
MASTER_MODEL = os.getenv("AI_WORK_OS_CLAUDE_MODEL", "").strip() or None
STORAGE = Path(os.getenv("AI_WORK_OS_STORAGE", "./storage")).resolve()
POLL_SECONDS = max(0.1, float(os.getenv("AI_WORK_OS_MASTER_POLL_SECONDS", "1")))
TURN_TIMEOUT_SECONDS = max(30, int(os.getenv("AI_WORK_OS_MASTER_TURN_TIMEOUT_SECONDS", "1800")))
TURN_LEASE_SECONDS = max(30, int(os.getenv("AI_WORK_OS_MASTER_TURN_LEASE_SECONDS", "60")))
HEARTBEAT_SECONDS = max(5, min(
    TURN_LEASE_SECONDS // 2,
    int(os.getenv("AI_WORK_OS_MASTER_HEARTBEAT_SECONDS", "10")),
))
MAX_TURNS = max(1, min(100, int(os.getenv("AI_WORK_OS_MASTER_MAX_TURNS", "12"))))
MAX_EVENT_BYTES = 64_000

SYSTEM_PROMPT = """你是 AI Work OS 的 Master Agent。
默认始终使用简体中文（zh-CN）与用户交流、解释、规划和汇报；只有用户明确要求其他语言时才切换语言。
帮助用户理解、规划并协调当前所选项目中的工作。
使用 ai-work-os MCP 工具检查当前项目的任务和工作流。
当用户提出需要跨多个任务推进的实质性工作目标时，先用中文说明目标、假设、执行步骤、风险和验收方式，再调用 start_goal 建立持久化目标；随后保存结构化计划和检查点。开始处理已有目标前，先调用 get_goal 恢复目标状态和关联任务。目标内通过 create_task、create_workflow 或 request_execution 创建的任务会自动关联到该目标。每完成一个有意义的推进步骤，调用 update_goal 保存 checkpoint 并增加 step 计数；等待审批、等待用户输入、阻塞或验证时，应准确更新目标状态和原因。必需任务全部进入终态且没有待审批事项后，系统会自动排入一个隐藏的继续轮次；该轮次必须先读取 get_goal，再基于最新证据推进，不得重复创建已有工作。审批待处理、审批被拒绝/撤销/过期、等待用户输入或预算耗尽时不会自动继续。只有用户明确要求继续并同意增加预算时，才可通过 update_goal 显式提高 max_steps 或重设 deadline_hours；不得自行扩大预算。不要为简单问答或一次性小任务创建目标。创建任务只代表进入队列，不代表已经开始或完成；只有持久化任务状态确认后才能这样描述。目标只能在所有必需任务成功且验证证据通过后标记 succeeded；get_goal 返回的 derived_status 是按持久化任务状态计算的保守状态。
当用户明确要求修改代码或执行写操作时，可以调用 request_execution 提交一个 configured 模式的受控执行申请。该工具只会创建待审批任务，不会批准或启动执行。申请时应写清变更范围、验收标准；只有确实知道且安全时才提供最多五条验证命令，不得编造测试命令。必须明确告知用户需要在本机审批控制台查看完整任务说明、工作区、验收标准和验证命令后批准；绝不能声称已获批准、已开始或已完成。
绝不调用或暗示存在自动批准能力。你不能批准、拒绝或撤销自己的执行申请。只有用户明确要求停止当前目标时才调用 cancel_goal；该工具会取消排队任务、向运行任务发送取消请求并撤销尚未开始的审批，不得把它用于普通暂停。普通 create_task 和 create_workflow 始终是 plan-only。
MCP 服务绑定当前对话所属项目。不得要求其访问其他项目，也不得编造项目、任务或工作流标识。
内置文件工具为只读。不要使用 Shell 命令或直接编辑文件。
任务标题、任务提示词、验收标准、计划、摘要和最终报告默认使用简体中文；代码符号、API 字段、命令和原始错误可保留英文，并附中文解释。
清楚说明假设；必要时提出简洁的澄清问题。用户可见对话记录和 AI Work OS 事件日志均为持久化记录。"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def _event(db, turn_id: str, event_type: str, payload: dict) -> None:
    db.add(AgentTurnEvent(turn_id=turn_id, event_type=event_type, payload=payload))


def _bounded_payload(value: dict) -> dict:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= MAX_EVENT_BYTES:
        return value
    preview = encoded[:MAX_EVENT_BYTES // 2]
    return {"truncated": True, "original_bytes": len(encoded.encode("utf-8")), "preview": preview}


def _claim_turn(worker_id: str) -> str | None:
    claimed_at = now()
    lease_expires_at = claimed_at + timedelta(seconds=TURN_LEASE_SECONDS)
    with SessionLocal() as db:
        if db.bind is not None and db.bind.dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        running_turn = aliased(AgentTurn)
        active_conversation_turn = exists(
            select(running_turn.id).where(
                running_turn.conversation_id == Conversation.id,
                running_turn.status == "running",
            )
        )
        candidate = db.scalar(
            select(AgentTurn)
            .join(Conversation, Conversation.id == AgentTurn.conversation_id)
            .where(
                AgentTurn.status == "queued",
                ~active_conversation_turn,
            )
            .order_by(AgentTurn.created_at, AgentTurn.id)
            .limit(1)
        )
        if candidate is None:
            return None
        result = db.execute(
            update(AgentTurn)
            .where(AgentTurn.id == candidate.id, AgentTurn.status == "queued")
            .values(
                status="running", worker_id=worker_id,
                lease_expires_at=lease_expires_at,
                started_at=claimed_at, finished_at=None,
                cancel_requested=False, error_summary=None,
            )
        )
        if result.rowcount != 1:
            db.rollback()
            return None
        _event(db, candidate.id, "turn.started", {
            "worker_id": worker_id,
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "lease_expires_at": lease_expires_at.isoformat(),
        })
        db.commit()
        return candidate.id


def recover_orphaned_turns() -> int:
    recovered_at = now()
    recovered = 0
    with SessionLocal() as db:
        candidates = list(db.scalars(select(AgentTurn).where(AgentTurn.status == "running")))
        for turn in candidates:
            lease = turn.lease_expires_at
            if lease is not None and lease.tzinfo is None:
                lease = lease.replace(tzinfo=timezone.utc)
            if lease is not None and lease > recovered_at:
                continue
            previous_worker = turn.worker_id
            result = db.execute(
                update(AgentTurn)
                .where(
                    AgentTurn.id == turn.id,
                    AgentTurn.status == "running",
                    AgentTurn.worker_id == previous_worker if previous_worker else AgentTurn.worker_id.is_(None),
                    AgentTurn.lease_expires_at == turn.lease_expires_at
                    if turn.lease_expires_at is not None else AgentTurn.lease_expires_at.is_(None),
                )
                .values(
                    status="failed", finished_at=recovered_at,
                    worker_id=None, lease_expires_at=None,
                    error_summary=(
                        "Master Agent Worker 租约已过期。本轮不会自动重放；"
                        "重试前请检查事件记录和 Claude 会话。"
                    ),
                )
            )
            if result.rowcount != 1:
                continue
            _event(db, turn.id, "turn.recovered", {
                "reason": "lease_expired",
                "previous_worker_id": previous_worker,
                "automatic_retry": False,
            })
            recovered += 1
        db.commit()
    return recovered


def enqueue_goal_continuations(limit: int = 20) -> int:
    """Queue one hidden Master Agent turn after every required goal task reaches a terminal state."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    scheduled = 0
    changed = False
    terminal_task_states = {"succeeded", "failed", "cancelled", "blocked"}
    denied_approval_states = {"rejected", "revoked", "expired", "invalidated"}
    with SessionLocal() as db:
        if db.bind is not None and db.bind.dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        runs = list(db.scalars(
            select(MasterRun)
            .where(MasterRun.active_key.is_not(None))
            .order_by(MasterRun.updated_at, MasterRun.id)
            .limit(limit)
        ))
        for run in runs:
            if run.status in {"waiting_input", "succeeded", "failed", "cancelled"}:
                continue
            stop_reason = (run.checkpoint or {}).get("stop_reason")
            if stop_reason in {"deadline_exceeded", "step_budget_exceeded"}:
                continue
            deadline = run.deadline_at
            if deadline is not None and deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            if deadline is not None and deadline <= now():
                run.status = "blocked"
                run.checkpoint = {**(run.checkpoint or {}), "stop_reason": "deadline_exceeded"}
                run.updated_at = now()
                changed = True
                continue
            if run.step_count >= run.max_steps:
                run.status = "blocked"
                run.checkpoint = {**(run.checkpoint or {}), "stop_reason": "step_budget_exceeded"}
                run.updated_at = now()
                changed = True
                continue
            conversation = db.get(Conversation, run.conversation_id)
            if conversation is None or conversation.status != "active" or conversation.project_id != run.project_id:
                continue
            pending_turn = db.scalar(select(AgentTurn.id).where(
                AgentTurn.conversation_id == conversation.id,
                AgentTurn.status.in_(["queued", "running"]),
            ).limit(1))
            if pending_turn is not None:
                continue
            rows = db.execute(
                select(MasterRunTask, Task, ExecutionApproval.status, ExecutionApproval.expires_at)
                .join(Task, Task.id == MasterRunTask.task_id)
                .outerjoin(ExecutionApproval, ExecutionApproval.task_id == Task.id)
                .where(
                    MasterRunTask.master_run_id == run.id,
                    Task.project_id == run.project_id,
                )
                .order_by(MasterRunTask.created_at, MasterRunTask.id)
            ).all()
            required = []
            for link, task, approval_status, approval_expires_at in rows:
                if not link.required:
                    continue
                if (
                    approval_status == "pending" and approval_expires_at is not None
                    and approval_expires_at <= now()
                ):
                    approval_status = "expired"
                required.append((link, task, approval_status))
            if not required:
                continue
            if any(approval_status == "pending" for _, _, approval_status in required):
                continue
            if any(approval_status in denied_approval_states for _, _, approval_status in required):
                continue
            if not all(task.status in terminal_task_states for _, task, _ in required):
                continue
            state = [{
                "task_id": task.id,
                "status": task.status,
                "verification_status": task.verification_status,
                "approval_status": approval_status,
                "finished_at": task.finished_at.isoformat() if task.finished_at else None,
            } for _, task, approval_status in required]
            signature = hashlib.sha256(
                json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            checkpoint = run.checkpoint if isinstance(run.checkpoint, dict) else {}
            if checkpoint.get("continuation_signature") == signature:
                continue
            message = ConversationMessage(
                conversation_id=conversation.id,
                role="user",
                content=(
                    "系统继续通知：目标关联任务的持久化状态已发生变化。请先调用 get_goal 检查当前目标、任务和验证证据，"
                    "然后继续推进尚未完成的工作。不要重复创建已经存在的任务；不要绕过审批；"
                    "只有所有必需任务的验证证据通过后才能将目标标记为成功。"
                    "如果需要用户审批或补充信息，请停止并明确说明。"
                ),
                message_metadata={
                    "internal": True,
                    "kind": "master_run_continuation",
                    "master_run_id": run.id,
                    "task_state_signature": signature,
                },
            )
            db.add(message)
            conversation.updated_at = now()
            db.flush()
            turn = AgentTurn(
                conversation_id=conversation.id,
                user_message_id=message.id,
                status="queued",
                adapter="claude-code",
                cli_session_id=conversation.cli_session_id,
            )
            db.add(turn)
            db.flush()
            db.add(AgentTurnEvent(
                turn_id=turn.id,
                event_type="turn.queued",
                payload={
                    "source": "master_run_continuation",
                    "master_run_id": run.id,
                    "task_state_signature": signature,
                },
            ))
            run.checkpoint = {
                **checkpoint,
                "continuation_signature": signature,
                "continuation_turn_id": turn.id,
                "continuation_scheduled_at": now().isoformat(),
            }
            run.updated_at = now()
            scheduled += 1
            changed = True
        if changed:
            db.commit()
    return scheduled


def _workspace_for_turn(conversation: Conversation, project: Project | None) -> Path:
    if project is not None:
        workspace = Path(project.workspace_path).expanduser().resolve(strict=True)
    else:
        configured = os.getenv("AI_WORK_OS_MASTER_WORKSPACE")
        workspace = (
            Path(configured).expanduser().resolve(strict=True)
            if configured else (STORAGE / "master-agent-workspace").resolve()
        )
        workspace.mkdir(parents=True, exist_ok=True)
    if not workspace.is_dir():
        raise NotADirectoryError(f"Master Agent workspace is not a directory: {workspace}")
    return workspace


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key == "ANTHROPIC_MODEL" or (
            key.startswith("ANTHROPIC_DEFAULT_") and key.endswith("_MODEL")
        ):
            env.pop(key, None)
    env.pop("AI_WORK_OS_CLAUDE_MODEL", None)
    env.pop("AI_WORK_OS_CLAUDE_SETTING_SOURCES", None)
    return env


def build_master_command(
    session_id: str | None, mcp_config_path: Path,
) -> list[str]:
    command = [
        CLAUDE_BIN, "--print",
        "--output-format", "stream-json", "--verbose",
        "--include-partial-messages",
        "--append-system-prompt", SYSTEM_PROMPT,
        "--permission-mode", "default",
        "--tools", "Read,Glob,Grep",
        "--permission-prompts", "none",
        "--restricted",
        "--strict-mcp-config", "--mcp-config", str(mcp_config_path),
        "--allowedTools", "mcp__awos__*",
        "--disallowedTools", "Bash", "Edit", "Write", "NotebookEdit",
        "--max-turns", str(MAX_TURNS),
    ]
    if MASTER_MODEL:
        command.extend(["--model", MASTER_MODEL])
    if session_id:
        command.extend(["--resume", session_id])
    return command


def _write_mcp_config(
    turn_dir: Path, project_id: str | None, *, broker_socket: bool = False,
) -> Path:
    path = turn_dir / "mcp-config.isolated.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            if project_id:
                server_env = (
                    {
                        "AI_WORK_OS_MCP_BROKER_SOCKET": "/run/awos-mcp.sock",
                        "AI_WORK_OS_MCP_TURN_ID": turn_dir.name,
                    }
                    if broker_socket else {
                        "AI_WORK_OS_MCP_PROJECT_ID": project_id,
                        "AI_WORK_OS_MCP_TURN_ID": turn_dir.name,
                    }
                )
                config = {"mcpServers": {"awos": {
                    "type": "stdio", "command": sys.executable,
                    "args": ["-m", "ai_work_os.mcp_control_plane"],
                    "env": server_env,
                }}}
            else:
                config = {"mcpServers": {}}
            stream.write(json.dumps(config, ensure_ascii=False) + "\n")
    finally:
        if fd >= 0:
            os.close(fd)
    return path



async def _handle_mcp_broker(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    project_id: str, turn_id: str,
) -> None:
    from .mcp_control_plane import ToolError, call_tool
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout=30)
        if not raw or len(raw) > 1_000_000:
            raise ToolError("invalid MCP broker request size")
        request = json.loads(raw)
        name = request.get("name")
        arguments = request.get("arguments", {})
        request_id = request.get("request_id")
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise ToolError("invalid MCP broker request")
        if name in {"create_task", "create_workflow", "request_execution", "start_goal", "update_goal", "cancel_goal"} and request_id is None:
            raise ToolError("mutating MCP broker calls require a JSON-RPC request id")
        value = await asyncio.to_thread(
            call_tool, name, arguments, request_id,
            project_id=project_id, turn_id=turn_id,
        )
        response = {"ok": True, "value": value}
    except ToolError as exc:
        response = {"ok": False, "error": f"MCP 工具调用失败：{exc}"[:2000]}
    except Exception as exc:
        response = {"ok": False, "error": f"MCP broker 内部错误：{type(exc).__name__}"}
    try:
        writer.write(json.dumps(response, ensure_ascii=False, default=str).encode("utf-8") + b"\n")
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def _start_mcp_broker(turn_dir: Path, project_id: str, workspace: Path):
    socket_path = Path("/tmp") / f"awos-mcp-{turn_dir.name}.sock"
    try:
        socket_path.relative_to(workspace.resolve(strict=True))
    except ValueError:
        pass
    else:
        socket_path = Path("/var/tmp") / f"awos-mcp-{turn_dir.name}.sock"
    try:
        socket_path.relative_to(workspace.resolve(strict=True))
    except ValueError:
        pass
    else:
        raise ValueError("cannot place MCP broker socket outside the project workspace")
    if len(str(socket_path).encode()) >= 104:
        raise ValueError("MCP broker socket path is too long for Unix domain sockets")
    if socket_path.is_symlink():
        raise ValueError("MCP broker socket path must not be a symlink")
    if socket_path.exists():
        if not socket_path.is_socket():
            raise ValueError("MCP broker socket path is occupied by a non-socket file")
        socket_path.unlink()
    server = await asyncio.start_unix_server(
        lambda reader, writer: _handle_mcp_broker(reader, writer, project_id, turn_dir.name),
        path=str(socket_path), limit=1_000_001,
    )
    try:
        os.chmod(socket_path, 0o600)
    except OSError:
        server.close()
        await server.wait_closed()
        socket_path.unlink(missing_ok=True)
        raise
    return server, socket_path


def _mcp_runtime_paths() -> tuple[Path, ...]:
    """Read-only Python/runtime roots needed by the sandboxed stdio MCP client."""
    paths = {
        Path(sys.executable).resolve(strict=True),
        Path(sys.prefix).resolve(strict=True),
        Path(sys.base_prefix).resolve(strict=True),
        Path(__file__).resolve(strict=True).parent.parent,
    }
    for value in [*site.getsitepackages(), site.getusersitepackages()]:
        candidate = Path(value).expanduser()
        if candidate.exists():
            paths.add(candidate.resolve(strict=True))
    return tuple(sorted(paths, key=lambda item: str(item)))

def _safe_cli_event_payload(event_type: str, payload: dict) -> dict:
    """Persist CLI event metadata only; never retain file contents, prompts, or tool arguments."""
    if event_type == "assistant.delta":
        value = payload.get("text")
        return {"char_count": len(value)} if isinstance(value, str) else {}

    if event_type == "claude.non_json_output":
        value = payload.get("line")
        return {"line_char_count": len(value)} if isinstance(value, str) else {}

    safe_keys = {
        "session_id", "subtype", "tool_use_id", "tool_name", "name",
        "status", "is_error", "stop_reason", "num_turns", "duration_ms",
        "duration_api_ms", "total_cost_usd", "usage", "model",
        "permission_mode", "error_code",
    }
    safe = {key: payload[key] for key in safe_keys if key in payload}
    message = payload.get("message")
    if isinstance(message, dict):
        if isinstance(message.get("stop_reason"), str):
            safe["stop_reason"] = message["stop_reason"]
        if isinstance(message.get("model"), str):
            safe["model"] = message["model"]
        blocks = message.get("content")
        if isinstance(blocks, list):
            tools = []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    tools.append({
                        key: block[key] for key in ("id", "name")
                        if isinstance(block.get(key), str)
                    })
                elif block.get("type") == "tool_result":
                    content = block.get("content")
                    tools.append({
                        "tool_use_id": block.get("tool_use_id"),
                        "result_char_count": len(content) if isinstance(content, str) else None,
                        "is_error": bool(block.get("is_error", False)),
                    })
            if tools:
                safe["tool_activity"] = tools
    return safe


def _persist_event(
    turn_id: str, worker_id: str, event_type: str, payload: dict,
) -> bool:
    observed_at = now()
    safe_payload = (
        _safe_cli_event_payload(event_type, payload)
        if event_type.startswith("claude.") or event_type == "assistant.delta"
        else payload
    )
    with SessionLocal() as db:
        # Fence every event write just like lease renewal and turn completion.
        # A stale process must not append records after its lease expires or is replaced.
        result = db.execute(
            update(AgentTurn)
            .where(
                AgentTurn.id == turn_id,
                AgentTurn.status == "running",
                AgentTurn.worker_id == worker_id,
                AgentTurn.lease_expires_at > observed_at,
            )
            .values(updated_at=AgentTurn.updated_at)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            db.rollback()
            return False
        _event(db, turn_id, event_type, _bounded_payload(safe_payload))
        db.commit()
        return True


def _extract_text(event: dict) -> str | None:
    event_type = event.get("type")
    if event_type == "result":
        if bool(event.get("is_error")):
            return None
        value = event.get("result")
        return value if isinstance(value, str) and value.strip() else None
    if event_type == "assistant":
        message = event.get("message") or {}
        if message.get("stop_reason") != "end_turn":
            return None
        blocks = message.get("content") or []
        text = "\n".join(
            str(block.get("text", "")) for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        return text or None
    return None


def _persist_session_id(turn_id: str, worker_id: str, session_id: str) -> None:
    observed_at = now()
    with SessionLocal() as db:
        turn = db.get(AgentTurn, turn_id)
        if turn is None:
            return
        result = db.execute(
            update(AgentTurn)
            .where(
                AgentTurn.id == turn_id,
                AgentTurn.status == "running",
                AgentTurn.worker_id == worker_id,
                AgentTurn.lease_expires_at > observed_at,
            )
            .values(cli_session_id=session_id)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            db.rollback()
            return
        conversation = db.get(Conversation, turn.conversation_id)
        if conversation is None:
            db.rollback()
            return
        conversation.cli_session_id = session_id
        conversation.updated_at = observed_at
        _event(db, turn_id, "turn.session_attached", {"session_id": session_id})
        db.commit()

def _renew_lease(turn_id: str, worker_id: str) -> bool:
    renewed_at = now()
    with SessionLocal() as db:
        result = db.execute(
            update(AgentTurn)
            .where(
                AgentTurn.id == turn_id,
                AgentTurn.status == "running",
                AgentTurn.worker_id == worker_id,
                AgentTurn.lease_expires_at > renewed_at,
            )
            .values(lease_expires_at=renewed_at + timedelta(seconds=TURN_LEASE_SECONDS))
        )
        db.commit()
        return result.rowcount == 1


def _cancel_requested(turn_id: str, worker_id: str) -> bool:
    with SessionLocal() as db:
        turn = db.get(AgentTurn, turn_id)
        return bool(
            turn and turn.status == "running"
            and turn.worker_id == worker_id and turn.cancel_requested
        )


async def _terminate(proc: asyncio.subprocess.Process, grace_seconds: float = 4.0) -> bool:
    if os.name == "posix":
        pgid = proc.pid
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
            except asyncio.TimeoutError:
                pass

        async def group_exists() -> bool:
            try:
                os.killpg(pgid, 0)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                return True

        deadline = asyncio.get_running_loop().time() + grace_seconds
        while await group_exists() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        if await group_exists():
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                return False
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    return False
            deadline = asyncio.get_running_loop().time() + 2.0
            while await group_exists() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
        return not await group_exists()
    if proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
    return False


def _finish_turn(
    turn_id: str, worker_id: str, status: str, *,
    assistant_text: str | None = None, error: str | None = None,
    exit_code: int | None = None,
) -> None:
    finished_at = now()
    with SessionLocal() as db:
        turn = db.get(AgentTurn, turn_id)
        if turn is None or turn.status != "running" or turn.worker_id != worker_id:
            return
        assistant_message_id = None
        if status == "succeeded" and assistant_text:
            conversation = db.get(Conversation, turn.conversation_id)
            if conversation is not None:
                conversation.updated_at = finished_at
            message = ConversationMessage(
                conversation_id=turn.conversation_id,
                role="assistant",
                content=assistant_text,
                message_metadata={"turn_id": turn_id, "adapter": "claude-code"},
            )
            db.add(message)
            db.flush()
            assistant_message_id = message.id
        result = db.execute(
            update(AgentTurn)
            .where(
                AgentTurn.id == turn_id,
                AgentTurn.status == "running",
                AgentTurn.worker_id == worker_id,
                AgentTurn.lease_expires_at > finished_at,
            )
            .values(
                status=status,
                finished_at=finished_at,
                updated_at=finished_at,
                worker_id=None,
                lease_expires_at=None,
                error_summary=error,
                assistant_message_id=assistant_message_id,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            db.rollback()
            return
        _event(db, turn_id, "turn.finished", {
            "status": status, "exit_code": exit_code,
            "assistant_message_id": assistant_message_id,
            "error_summary": error,
        })
        db.commit()

async def execute_turn(turn_id: str, worker_id: str) -> None:
    proc: asyncio.subprocess.Process | None = None
    broker_server: asyncio.AbstractServer | None = None
    broker_socket_host: Path | None = None
    final_text: str | None = None
    exit_code: int | None = None
    error: str | None = None
    terminal_status = "failed"
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    try:
        with SessionLocal() as db:
            turn = db.get(AgentTurn, turn_id)
            if turn is None or turn.status != "running" or turn.worker_id != worker_id:
                return
            conversation = db.get(Conversation, turn.conversation_id)
            user_message = db.get(ConversationMessage, turn.user_message_id)
            project = db.get(Project, conversation.project_id) if conversation and conversation.project_id else None
            session_id = conversation.cli_session_id or turn.cli_session_id
            if conversation is None or user_message is None:
                raise RuntimeError("AgentTurn is missing its conversation or user message")
            prompt = user_message.content

        workspace = _workspace_for_turn(conversation, project)
        turn_dir = STORAGE / "master-agent" / turn_id
        turn_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = turn_dir / "stdout.jsonl"
        stderr_path = turn_dir / "stderr.log"
        stderr_bytes = 0
        use_mcp_broker = bool(project is not None and sandbox_mode() == "bubblewrap")
        if use_mcp_broker:
            broker_server, broker_socket_host = await _start_mcp_broker(turn_dir, project.id, workspace)
        mcp_config = _write_mcp_config(
            turn_dir, project.id if project else None, broker_socket=use_mcp_broker,
        )
        claude_config_dir = STORAGE / "master-agent" / "claude-config" / conversation.id
        claude_config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if claude_config_dir.is_symlink():
            raise ValueError("persistent Claude config directory must not be a symlink")
        os.chmod(claude_config_dir, 0o700)
        command = build_master_command(session_id, mcp_config)
        subprocess_env = _subprocess_env()
        subprocess_env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir)
        invocation = build_sandbox_invocation(
            command, workspace, turn_dir, "plan", subprocess_env,
            mcp_config_path=mcp_config, execution_kind="master_agent",
            claude_config_dir=claude_config_dir,
            mcp_broker_socket_path=broker_socket_host,
            mcp_runtime_paths=_mcp_runtime_paths() if broker_socket_host else (),
        )
        _persist_event(turn_id, worker_id, "turn.executor_started", {
            "command": [
                "<claude>", "--print", "--output-format", "stream-json",
                "--verbose", "--include-partial-messages", "--permission-mode", "default",
                "--tools", "Read,Glob,Grep", "--permission-prompts", "none", "--setting-sources", "project,local",
                "--strict-mcp-config", "--mcp-config", "<isolated-mcp-config>",
                "--allowedTools", "mcp__awos__*",
                "--disallowedTools", "Bash", "Edit", "Write", "NotebookEdit",
                "--max-turns", str(MAX_TURNS),
                *(["--resume", "<persisted-session-id>"] if session_id else []),
            ],
            "workspace": str(workspace),
            "claude_config_dir": str(claude_config_dir),
            "permission_mode": "default",
            "configured_model": MASTER_MODEL or "cli_default",
            "tool_policy": "read_only_builtin_tools_with_allowlisted_project_mcp",
            "mcp_policy": "conversation_project_allowlist" if project else "disabled_projectless",
            "sandbox": invocation.metadata,
            "timeout_seconds": TURN_TIMEOUT_SECONDS,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        })
        with stdout_path.open("w", encoding="utf-8") as out, stderr_path.open("w", encoding="utf-8") as err:
            proc = await asyncio.create_subprocess_exec(
                *invocation.command,
                cwd=invocation.cwd,
                env=invocation.env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=1024 * 1024,
                start_new_session=(os.name == "posix"),
            )
            assert proc.stdin is not None
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            await proc.stdin.wait_closed()

            async def read_stdout() -> None:
                nonlocal final_text
                assert proc is not None and proc.stdout is not None
                while line := await proc.stdout.readline():
                    decoded = line.decode("utf-8", errors="replace")
                    try:
                        event = json.loads(decoded)
                    except json.JSONDecodeError:
                        out.write(json.dumps({
                            "event_type": "claude.non_json_output",
                            "payload": {"line_char_count": len(decoded)},
                        }) + "\n")
                        out.flush()
                        _persist_event(turn_id, worker_id, "claude.non_json_output", {"line": decoded[:4000]})
                        continue
                    event_type = str(event.get("type", "unknown"))
                    safe_payload = _safe_cli_event_payload(f"claude.{event_type}", event)
                    out.write(json.dumps({
                        "event_type": f"claude.{event_type}", "payload": safe_payload,
                    }, ensure_ascii=False) + "\n")
                    out.flush()
                    observed_session = event.get("session_id")
                    if observed_session:
                        _persist_session_id(turn_id, worker_id, str(observed_session))
                    extracted = _extract_text(event)
                    if extracted:
                        final_text = extracted
                    if event_type == "stream_event":
                        nested = event.get("event") or {}
                        delta = nested.get("delta") or {}
                        if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                            _persist_event(turn_id, worker_id, "assistant.delta", {"text": delta["text"]})
                    elif event_type in {"system", "assistant", "result", "user", "tool_use", "tool_result"}:
                        _persist_event(turn_id, worker_id, f"claude.{event_type}", event)

            async def read_stderr() -> None:
                nonlocal stderr_bytes
                assert proc is not None and proc.stderr is not None
                while chunk := await proc.stderr.read(4096):
                    stderr_bytes += len(chunk)
                err.write(json.dumps({"stderr_bytes": stderr_bytes}) + "\n")
                err.flush()

            readers = [asyncio.create_task(read_stdout()), asyncio.create_task(read_stderr())]
            deadline = asyncio.get_running_loop().time() + TURN_TIMEOUT_SECONDS
            next_renewal = asyncio.get_running_loop().time() + HEARTBEAT_SECONDS
            while proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=0.2)
                except asyncio.TimeoutError:
                    pass
                if proc.returncode is not None:
                    break
                if _cancel_requested(turn_id, worker_id):
                    stopped = await _terminate(proc)
                    terminal_status = "cancelled"
                    error = "用户请求取消本轮对话。"
                    _persist_event(turn_id, worker_id, "turn.cancelled", {
                        "reason": "cancel_requested",
                        "process_tree_stopped": stopped,
                    })
                    break
                current = asyncio.get_running_loop().time()
                if current >= deadline:
                    stopped = await _terminate(proc)
                    terminal_status = "failed"
                    error = f"Master Agent 处理超时（{TURN_TIMEOUT_SECONDS} 秒）。"
                    _persist_event(turn_id, worker_id, "turn.timed_out", {
                        "timeout_seconds": TURN_TIMEOUT_SECONDS,
                        "process_tree_stopped": stopped,
                    })
                    break
                if current >= next_renewal:
                    if not _renew_lease(turn_id, worker_id):
                        stopped = await _terminate(proc)
                        error = "Master Agent Worker 租约丢失。"
                        _persist_event(turn_id, worker_id, "turn.lease_lost", {
                            "process_tree_stopped": stopped,
                        })
                        await asyncio.gather(*readers, return_exceptions=True)
                        return
                    next_renewal = current + HEARTBEAT_SECONDS
            await asyncio.gather(*readers, return_exceptions=True)
            exit_code = proc.returncode
            if terminal_status == "cancelled":
                pass
            elif exit_code == 0 and final_text:
                terminal_status = "succeeded"
            else:
                error = (
                    f"Claude Code 退出码为 {exit_code}，但未返回最终回复。"
                    if exit_code == 0 else f"Claude Code 退出，状态码：{exit_code}"
                )
                if stderr_bytes:
                    error += f": 检测到标准错误输出（{stderr_bytes} 字节；为保护隐私未保存内容）"
        _finish_turn(
            turn_id, worker_id, terminal_status,
            assistant_text=final_text if terminal_status == "succeeded" else None,
            error=error, exit_code=exit_code,
        )
    except asyncio.CancelledError:
        if proc is not None:
            stopped = await _terminate(proc)
            _persist_event(turn_id, worker_id, "turn.worker_interrupted", {
                "reason": "worker_shutdown", "process_tree_stopped": stopped,
            })
        _finish_turn(
            turn_id, worker_id, "failed",
            error="Master Agent Worker 中断；重试前请检查 CLI 会话状态。",
            exit_code=proc.returncode if proc is not None else None,
        )
        raise
    except Exception as exc:
        error = f"Master Agent 执行失败（{type(exc).__name__}）：{exc}"
        _persist_event(turn_id, worker_id, "turn.executor_failed", {"error": error})
        _finish_turn(turn_id, worker_id, "failed", error=error, exit_code=exit_code)
    finally:
        if broker_server is not None:
            broker_server.close()
            await broker_server.wait_closed()
        if broker_socket_host is not None:
            try:
                broker_socket_host.unlink(missing_ok=True)
            except OSError:
                pass


async def run_master_agent() -> None:
    worker_id = str(uuid.uuid4())
    recovered = recover_orphaned_turns()
    print(
        f"AI Work OS Master Agent started; worker_id={worker_id}; "
        f"lease_seconds={TURN_LEASE_SECONDS}; heartbeat_seconds={HEARTBEAT_SECONDS}; "
        f"recovered_turns={recovered}",
        flush=True,
    )
    active: set[asyncio.Task] = set()
    try:
        while True:
            active = {job for job in active if not job.done()}
            try:
                enqueue_goal_continuations()
            except Exception as exc:
                print(f"MasterRun continuation scheduler error: {type(exc).__name__}", flush=True)
            if not active:
                turn_id = _claim_turn(worker_id)
                if turn_id:
                    active.add(asyncio.create_task(execute_turn(turn_id, worker_id)))
            if active:
                await asyncio.wait(active, timeout=POLL_SECONDS, return_when=asyncio.FIRST_COMPLETED)
            else:
                await asyncio.sleep(POLL_SECONDS)
    finally:
        for job in active:
            job.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(run_master_agent())
    except KeyboardInterrupt:
        pass
