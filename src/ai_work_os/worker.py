from __future__ import annotations
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import select, update
from .db import Base, SessionLocal, engine
from .models import Task, TaskEvent, elapsed_ms

Base.metadata.create_all(bind=engine)
STORAGE = Path(os.getenv("AI_WORK_OS_STORAGE", "./storage")).resolve()
CLAUDE_BIN = os.getenv("AI_WORK_OS_CLAUDE_BIN", "claude")
POLL_SECONDS = float(os.getenv("AI_WORK_OS_POLL_SECONDS", "1"))
MAX_TASK_SECONDS = int(os.getenv("AI_WORK_OS_MAX_TASK_SECONDS", "3600"))


def now() -> datetime:
    return datetime.now(timezone.utc)


def add_event(task_id: str, event_type: str, payload: dict) -> None:
    with SessionLocal() as db:
        db.add(TaskEvent(task_id=task_id, event_type=event_type, payload=payload))
        db.commit()


def claim_task() -> str | None:
    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.status == "queued").order_by(Task.created_at).limit(1))
        if task is None:
            return None
        started_at = now()
        result = db.execute(
            update(Task)
            .where(Task.id == task.id, Task.status == "queued")
            .values(status="running", started_at=started_at)
        )
        if result.rowcount != 1:
            db.rollback()
            return None
        task_id = task.id
        db.add(TaskEvent(
            task_id=task_id,
            event_type="executor.claimed",
            payload={
                "queue_wait_ms": elapsed_ms(task.created_at, started_at),
                "queued_at": task.created_at.isoformat(),
                "claimed_at": started_at.isoformat(),
            },
        ))
        db.commit()
        return task_id


async def execute_task(task_id: str) -> None:
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is None:
            return
        workspace = Path(task.workspace_path)
        prompt = task.prompt

    task_dir = STORAGE / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    stdout_path, stderr_path = task_dir / "stdout.jsonl", task_dir / "stderr.log"
    command = [CLAUDE_BIN, "-p", prompt, "--output-format", "stream-json", "--verbose", "--permission-prompts", "none"]
    add_event(task_id, "executor.started", {"command": [CLAUDE_BIN, "-p", "<prompt>", "--output-format", "stream-json", "--verbose", "--permission-prompts", "none"], "workspace": str(workspace)})
    exit_code, error_summary, session_id = None, None, None

    try:
        with stdout_path.open("w", encoding="utf-8") as out, stderr_path.open("w", encoding="utf-8") as err:
            proc = await asyncio.create_subprocess_exec(*command, cwd=str(workspace), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

            async def read_stdout() -> None:
                nonlocal session_id
                assert proc.stdout is not None
                while line := await proc.stdout.readline():
                    decoded = line.decode("utf-8", errors="replace")
                    out.write(decoded)
                    out.flush()
                    try:
                        event = json.loads(decoded)
                        event_type = str(event.get("type", "unknown"))
                        if event_type == "result" and event.get("session_id"):
                            session_id = str(event["session_id"])
                        add_event(task_id, f"claude.{event_type}", event)
                    except json.JSONDecodeError:
                        add_event(task_id, "claude.non_json_output", {"line": decoded[:4000]})

            async def read_stderr() -> None:
                assert proc.stderr is not None
                while chunk := await proc.stderr.read(4096):
                    err.write(chunk.decode("utf-8", errors="replace"))
                    err.flush()

            readers = [asyncio.create_task(read_stdout()), asyncio.create_task(read_stderr())]
            deadline = asyncio.get_running_loop().time() + MAX_TASK_SECONDS
            try:
                while proc.returncode is None:
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        with SessionLocal() as db:
                            current = db.get(Task, task_id)
                            cancel_requested = bool(current and current.cancel_requested)
                        if cancel_requested:
                            proc.terminate()
                            try:
                                await asyncio.wait_for(proc.wait(), timeout=5)
                            except asyncio.TimeoutError:
                                proc.kill()
                                await proc.wait()
                            error_summary = "Cancelled by request"
                            break
                        if asyncio.get_running_loop().time() >= deadline:
                            proc.terminate()
                            try:
                                await asyncio.wait_for(proc.wait(), timeout=5)
                            except asyncio.TimeoutError:
                                proc.kill()
                                await proc.wait()
                            error_summary = f"Task timed out after {MAX_TASK_SECONDS} seconds"
                            break
            finally:
                await asyncio.gather(*readers, return_exceptions=True)
            exit_code = proc.returncode
    except FileNotFoundError:
        error_summary, exit_code = f"Claude Code executable not found: {CLAUDE_BIN}", 127
    except Exception as exc:
        error_summary, exit_code = f"{type(exc).__name__}: {exc}", 1

    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is None:
            return
        task.exit_code, task.session_id, task.finished_at = exit_code, session_id, now()
        if task.cancel_requested:
            task.status = "cancelled"
        elif exit_code == 0 and error_summary is None:
            task.status = "succeeded"
        else:
            task.status = "failed"
            task.error_summary = error_summary or f"Claude Code exited with status {exit_code}"
        db.add(TaskEvent(task_id=task_id, event_type="executor.finished", payload={"status": task.status, "exit_code": exit_code, "session_id": session_id, "error_summary": task.error_summary, "stdout_path": str(stdout_path), "stderr_path": str(stderr_path)}))
        db.commit()


async def run_worker() -> None:
    print("AI Work OS worker started; waiting for queued tasks.", flush=True)
    while True:
        task_id = claim_task()
        if task_id is None:
            await asyncio.sleep(POLL_SECONDS)
        else:
            await execute_task(task_id)


if __name__ == "__main__":
    asyncio.run(run_worker())
