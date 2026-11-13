from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update

from .db import Base, SessionLocal, engine, ensure_schema_compatibility
from .models import (
    AgentTurn, AgentTurnEvent, Conversation, ConversationMessage, Project, utcnow,
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

SYSTEM_PROMPT = """You are the Master Agent for AI Work OS.
Help the user understand, plan, and coordinate work in the selected project.
Use the ai-work-os MCP tools to inspect the current project's tasks and workflows.
When the user asks you to delegate or organize work, decompose it into clear tasks
and dependencies, explain the proposed plan, then create queued plan-mode tasks or
workflows using the MCP tools. Creating work only queues it; never claim it has
started or completed until persisted task state confirms that.
The MCP server is bound to this conversation's project. Never ask it to access
another project, and never invent project, task, or workflow identifiers.
Built-in file tools are read-only. Do not use shell commands or edit files.
Be explicit about assumptions and ask concise clarification questions when needed.
The user-visible transcript and AI Work OS event log are durable records."""


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
        candidate = db.scalar(
            select(AgentTurn)
            .where(AgentTurn.status == "queued")
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
                        "Master Agent worker lease expired. The turn was not replayed automatically; "
                        "inspect its events and Claude session before retrying."
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
        "--permission-mode", "plan",
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


def _write_mcp_config(turn_dir: Path, project_id: str | None) -> Path:
    path = turn_dir / "mcp-config.isolated.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            if project_id:
                config = {"mcpServers": {"awos": {
                    "type": "stdio", "command": sys.executable,
                    "args": ["-m", "ai_work_os.mcp_control_plane"],
                    "env": {"AI_WORK_OS_MCP_PROJECT_ID": project_id},
                }}}
            else:
                config = {"mcpServers": {}}
            stream.write(json.dumps(config, ensure_ascii=False) + "\\n")
    finally:
        if fd >= 0:
            os.close(fd)
    return path


def _persist_event(turn_id: str, event_type: str, payload: dict) -> None:
    with SessionLocal() as db:
        _event(db, turn_id, event_type, _bounded_payload(payload))
        db.commit()


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
        if turn is None or turn.cli_session_id == session_id:
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
            session_id = turn.cli_session_id
            if conversation is None or user_message is None:
                raise RuntimeError("AgentTurn is missing its conversation or user message")
            prompt = user_message.content

        workspace = _workspace_for_turn(conversation, project)
        turn_dir = STORAGE / "master-agent" / turn_id
        turn_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = turn_dir / "stdout.jsonl"
        stderr_path = turn_dir / "stderr.log"
        if project is not None and sandbox_mode() == "bubblewrap":
            raise RuntimeError(
                "Project-scoped MCP is not yet supported inside Bubblewrap; refusing to expose the host database or runtime"
            )
        mcp_config = _write_mcp_config(turn_dir, project.id if project else None)
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
            mcp_config_path=mcp_config, execution_kind="task",
            claude_config_dir=claude_config_dir,
        )
        _persist_event(turn_id, "turn.executor_started", {
            "command": [
                "<claude>", "--print", "--output-format", "stream-json",
                "--verbose", "--include-partial-messages", "--permission-mode", "plan",
                "--tools", "Read,Glob,Grep", "--permission-prompts", "none", "--setting-sources", "project,local",
                "--strict-mcp-config", "--mcp-config", "<isolated-mcp-config>",
                "--allowedTools", "mcp__awos__*",
                "--disallowedTools", "Bash", "Edit", "Write", "NotebookEdit",
                "--max-turns", str(MAX_TURNS),
                *(["--resume", "<persisted-session-id>"] if session_id else []),
            ],
            "workspace": str(workspace),
            "claude_config_dir": str(claude_config_dir),
            "permission_mode": "plan",
            "configured_model": MASTER_MODEL or "cli_default",
            "tool_policy": "read_only_builtin_tools",
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
                    out.write(decoded)
                    out.flush()
                    try:
                        event = json.loads(decoded)
                    except json.JSONDecodeError:
                        _persist_event(turn_id, "claude.non_json_output", {"line": decoded[:4000]})
                        continue
                    event_type = str(event.get("type", "unknown"))
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
                            _persist_event(turn_id, "assistant.delta", {"text": delta["text"]})
                    elif event_type in {"system", "assistant", "result", "user", "tool_use", "tool_result"}:
                        _persist_event(turn_id, f"claude.{event_type}", event)

            async def read_stderr() -> None:
                assert proc is not None and proc.stderr is not None
                while chunk := await proc.stderr.read(4096):
                    err.write(chunk.decode("utf-8", errors="replace"))
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
                    error = "Cancelled by request"
                    _persist_event(turn_id, "turn.cancelled", {
                        "reason": "cancel_requested",
                        "process_tree_stopped": stopped,
                    })
                    break
                current = asyncio.get_running_loop().time()
                if current >= deadline:
                    stopped = await _terminate(proc)
                    terminal_status = "failed"
                    error = f"Master Agent turn timed out after {TURN_TIMEOUT_SECONDS} seconds"
                    _persist_event(turn_id, "turn.timed_out", {
                        "timeout_seconds": TURN_TIMEOUT_SECONDS,
                        "process_tree_stopped": stopped,
                    })
                    break
                if current >= next_renewal:
                    if not _renew_lease(turn_id, worker_id):
                        stopped = await _terminate(proc)
                        error = "Master Agent worker lost its turn lease"
                        _persist_event(turn_id, "turn.lease_lost", {
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
                stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")[-4000:]
                error = (
                    f"Claude Code exited with code {exit_code} without a final assistant response"
                    if exit_code == 0 else f"Claude Code exited with code {exit_code}"
                )
                if stderr_text.strip():
                    error += f": {stderr_text.strip()[-2000:]}"
        _finish_turn(
            turn_id, worker_id, terminal_status,
            assistant_text=final_text if terminal_status == "succeeded" else None,
            error=error, exit_code=exit_code,
        )
    except asyncio.CancelledError:
        if proc is not None:
            stopped = await _terminate(proc)
            _persist_event(turn_id, "turn.worker_interrupted", {
                "reason": "worker_shutdown", "process_tree_stopped": stopped,
            })
        _finish_turn(
            turn_id, worker_id, "failed",
            error="Master Agent worker interrupted; inspect the CLI session before retrying",
            exit_code=proc.returncode if proc is not None else None,
        )
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        _persist_event(turn_id, "turn.executor_failed", {"error": error})
        _finish_turn(turn_id, worker_id, "failed", error=error, exit_code=exit_code)


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
