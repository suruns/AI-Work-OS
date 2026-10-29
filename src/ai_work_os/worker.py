from __future__ import annotations
import asyncio
import hashlib
import json
import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import select, update
from .db import Base, SessionLocal, engine, ensure_schema_compatibility
from .models import Task, TaskEvent, elapsed_ms

Base.metadata.create_all(bind=engine)
ensure_schema_compatibility()
STORAGE = Path(os.getenv("AI_WORK_OS_STORAGE", "./storage")).resolve()
CLAUDE_BIN = os.getenv("AI_WORK_OS_CLAUDE_BIN", "claude")
POLL_SECONDS = float(os.getenv("AI_WORK_OS_POLL_SECONDS", "1"))
MAX_TASK_SECONDS = int(os.getenv("AI_WORK_OS_MAX_TASK_SECONDS", "3600"))


def now() -> datetime:
    return datetime.now(timezone.utc)


def build_command(prompt: str, permission_mode: str) -> list[str]:
    command = [CLAUDE_BIN, "-p", prompt, "--output-format", "stream-json", "--verbose", "--permission-prompts", "none"]
    if permission_mode == "plan":
        command.extend(["--permission-mode", "plan"])
    return command


async def terminate_process_tree(proc: asyncio.subprocess.Process, grace_seconds: float = 5.0) -> None:
    """Stop the agent and any subprocesses it spawned, escalating if needed."""
    if proc.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except ProcessLookupError:
        pass
    except (PermissionError, OSError):
        if proc.returncode is None:
            proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
    except asyncio.TimeoutError:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


async def _git_output(workspace: Path, *args: str) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(workspace), *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed with exit code {proc.returncode}")
    return stdout


def _hash_path(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_symlink():
        digest.update(b"symlink\\0")
        digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
    else:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


async def capture_workspace_snapshot(workspace: Path) -> dict:
    """Capture a Git workspace's net diff state without storing source contents."""
    try:
        root = Path((await _git_output(workspace, "rev-parse", "--show-toplevel")).decode().strip()).resolve()
        tracked_paths = (await _git_output(root, "diff", "--name-only", "-z", "HEAD")).split(b"\\0")
        untracked_paths = (await _git_output(root, "ls-files", "--others", "--exclude-standard", "-z")).split(b"\\0")
        tracked = {}
        for raw_path in filter(None, tracked_paths):
            rel = raw_path.decode("utf-8", errors="surrogateescape")
            patch = await _git_output(root, "diff", "--binary", "HEAD", "--", rel)
            tracked[rel] = hashlib.sha256(patch).hexdigest()
        untracked = {}
        for raw_path in filter(None, untracked_paths):
            rel = raw_path.decode("utf-8", errors="surrogateescape")
            try:
                untracked[rel] = _hash_path(root / rel)
            except (FileNotFoundError, PermissionError, OSError):
                untracked[rel] = "unreadable"
        return {"available": True, "repository_root": str(root), "tracked": tracked, "untracked": untracked}
    except (FileNotFoundError, RuntimeError, asyncio.TimeoutError, OSError) as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}


def compare_workspace_snapshots(before: dict, after: dict) -> dict:
    """Report net changes between snapshots and flag changes that predated the task."""
    if not before.get("available") or not after.get("available"):
        return {
            "available": False,
            "reason": "Workspace change tracking requires a Git repository and successful snapshots.",
            "before_reason": before.get("reason"),
            "after_reason": after.get("reason"),
        }
    before_files = {**before["tracked"], **before["untracked"]}
    after_files = {**after["tracked"], **after["untracked"]}
    changed = sorted(
        path for path in before_files.keys() | after_files.keys()
        if before_files.get(path) != after_files.get(path)
    )
    return {
        "available": True,
        "repository_root": after["repository_root"],
        "changed_during_task": changed,
        "preexisting_changes": sorted(before_files),
        "final_changes": sorted(after_files),
        "changes_reverted_or_removed": sorted(set(before_files) - set(after_files)),
        "note": "Snapshot comparison shows net changes only; edits that were made and then reverted during execution are not detectable. Pre-existing changes are listed separately.",
    }


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
        permission_mode = task.permission_mode

    task_dir = STORAGE / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    stdout_path, stderr_path = task_dir / "stdout.jsonl", task_dir / "stderr.log"
    workspace_before = await capture_workspace_snapshot(workspace)
    command = build_command(prompt, permission_mode)
    logged_command = [CLAUDE_BIN, "-p", "<prompt>", "--output-format", "stream-json", "--verbose", "--permission-prompts", "none"]
    if permission_mode == "plan":
        logged_command.extend(["--permission-mode", "plan"])
    add_event(task_id, "executor.started", {
        "command": logged_command,
        "workspace": str(workspace),
        "permission_mode": permission_mode,
    })
    exit_code, error_summary, session_id = None, None, None
    terminal_reason = None

    try:
        with stdout_path.open("w", encoding="utf-8") as out, stderr_path.open("w", encoding="utf-8") as err:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=(os.name == "posix"),
            )

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
                        await asyncio.wait_for(proc.wait(), timeout=0.25)
                    except asyncio.TimeoutError:
                        with SessionLocal() as db:
                            current = db.get(Task, task_id)
                            cancel_requested = bool(current and current.cancel_requested)
                        if cancel_requested:
                            await terminate_process_tree(proc)
                            terminal_reason = "cancelled"
                            error_summary = "Cancelled by request"
                            add_event(task_id, "executor.cancelled", {"reason": "cancel_requested"})
                            break
                        if asyncio.get_running_loop().time() >= deadline:
                            await terminate_process_tree(proc)
                            terminal_reason = "timeout"
                            error_summary = f"Task timed out after {MAX_TASK_SECONDS} seconds"
                            add_event(task_id, "executor.timed_out", {
                                "timeout_seconds": MAX_TASK_SECONDS,
                                "exit_code": proc.returncode,
                            })
                            break
            except asyncio.CancelledError:
                await terminate_process_tree(proc)
                terminal_reason = "worker_interrupted"
                error_summary = "Worker interrupted; Claude subprocess tree terminated"
                add_event(task_id, "executor.interrupted", {"reason": "worker_shutdown"})
            finally:
                await asyncio.gather(*readers, return_exceptions=True)
            exit_code = proc.returncode
    except FileNotFoundError:
        error_summary, exit_code = f"Claude Code executable not found: {CLAUDE_BIN}", 127
    except Exception as exc:
        error_summary, exit_code = f"{type(exc).__name__}: {exc}", 1

    workspace_after = await capture_workspace_snapshot(workspace)
    workspace_changes = compare_workspace_snapshots(workspace_before, workspace_after)
    workspace_report_path = task_dir / "workspace_changes.json"
    report_written = False
    try:
        workspace_report_path.write_text(
            json.dumps(workspace_changes, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        report_written = True
    except OSError as exc:
        workspace_changes["report_write_error"] = f"{type(exc).__name__}: {exc}"
    add_event(task_id, "workspace.changes_reported", {
        "available": workspace_changes["available"],
        "changed_file_count": len(workspace_changes.get("changed_during_task", [])),
        "report_path": str(workspace_report_path) if report_written else None,
        "report_written": report_written,
        "preexisting_file_count": len(workspace_changes.get("preexisting_changes", [])),
        "report_write_error": workspace_changes.get("report_write_error"),
    })

    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is None:
            return
        task.exit_code, task.session_id, task.finished_at = exit_code, session_id, now()
        if task.cancel_requested or terminal_reason == "cancelled":
            task.status = "cancelled"
        elif exit_code == 0 and error_summary is None:
            task.status = "succeeded"
        else:
            task.status = "failed"
            task.error_summary = error_summary or f"Claude Code exited with status {exit_code}"
        db.add(TaskEvent(task_id=task_id, event_type="executor.finished", payload={
            "status": task.status,
            "exit_code": exit_code,
            "session_id": session_id,
            "error_summary": task.error_summary,
            "terminal_reason": terminal_reason or ("success" if task.status == "succeeded" else "error"),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }))
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
