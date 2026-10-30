from __future__ import annotations
import asyncio
import hashlib
import json
import os
import signal
import shutil
import shlex
from datetime import datetime, timezone, timedelta
from pathlib import Path
from sqlalchemy import exists, or_, select, update
from .db import Base, SessionLocal, engine, ensure_schema_compatibility
from .models import Task, TaskEvent, Workflow, WorkflowTaskDependency, elapsed_ms

Base.metadata.create_all(bind=engine)
ensure_schema_compatibility()
STORAGE = Path(os.getenv("AI_WORK_OS_STORAGE", "./storage")).resolve()
CLAUDE_BIN = os.getenv("AI_WORK_OS_CLAUDE_BIN", "claude")
POLL_SECONDS = float(os.getenv("AI_WORK_OS_POLL_SECONDS", "1"))
MAX_TASK_SECONDS = int(os.getenv("AI_WORK_OS_MAX_TASK_SECONDS", "3600"))
MAX_CONCURRENT_TASKS = max(1, int(os.getenv("AI_WORK_OS_MAX_CONCURRENT_TASKS", "1")))
RETRY_BASE_DELAY_SECONDS = max(0.0, float(os.getenv("AI_WORK_OS_RETRY_BASE_DELAY_SECONDS", "2")))
WORKER_LEASE_SECONDS = max(30, int(os.getenv("AI_WORK_OS_WORKER_LEASE_SECONDS", "60")))
WORKER_HEARTBEAT_SECONDS = max(5, min(WORKER_LEASE_SECONDS // 2, int(os.getenv("AI_WORK_OS_WORKER_HEARTBEAT_SECONDS", "10"))))


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
        digest.update(b"symlink\0")
        digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
    else:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


async def capture_workspace_snapshot(workspace: Path, exclude_paths: list[Path] | None = None) -> dict:
    """Capture a Git workspace's net diff state without storing source contents."""
    try:
        root = Path((await _git_output(workspace, "rev-parse", "--show-toplevel")).decode().strip()).resolve()
        tracked_paths = (await _git_output(root, "diff", "--name-only", "-z", "HEAD")).split(b"\0")
        untracked_paths = (await _git_output(root, "ls-files", "--others", "--exclude-standard", "-z")).split(b"\0")
        def is_generated_python_cache(rel: str) -> bool:
            parts = Path(rel).parts
            return "__pycache__" in parts or rel.endswith(".pyc")

        tracked = {}
        for raw_path in filter(None, tracked_paths):
            rel = raw_path.decode("utf-8", errors="surrogateescape")
            if is_generated_python_cache(rel):
                continue
            patch = await _git_output(root, "diff", "--binary", "HEAD", "--", rel)
            tracked[rel] = hashlib.sha256(patch).hexdigest()
        untracked = {}
        excluded = [path.resolve() for path in (exclude_paths or [])]
        for raw_path in filter(None, untracked_paths):
            rel = raw_path.decode("utf-8", errors="surrogateescape")
            if is_generated_python_cache(rel):
                continue
            candidate = root / rel
            resolved_candidate = candidate.resolve()
            if any(resolved_candidate == item or item in resolved_candidate.parents for item in excluded):
                continue
            try:
                untracked[rel] = _hash_path(candidate)
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


def reconcile_workflows() -> None:
    """Idempotently block descendants of failed parents and aggregate workflow state."""
    with SessionLocal() as db:
        workflows = list(db.scalars(select(Workflow)))
        for workflow in workflows:
            tasks = list(db.scalars(select(Task).where(Task.workflow_id == workflow.id)))
            by_id = {task.id: task for task in tasks}
            edges = list(db.scalars(select(WorkflowTaskDependency).where(
                WorkflowTaskDependency.workflow_id == workflow.id
            )))
            parents: dict[str, list[str]] = {task.id: [] for task in tasks}
            for edge in edges:
                parents.setdefault(edge.downstream_task_id, []).append(edge.upstream_task_id)
            changed = True
            while changed:
                changed = False
                for task in tasks:
                    if task.status != "queued":
                        continue
                    failed_parent = next((
                        by_id[parent_id] for parent_id in parents.get(task.id, [])
                        if parent_id in by_id and by_id[parent_id].status in {"failed", "cancelled", "blocked"}
                    ), None)
                    if failed_parent is None:
                        continue
                    root_reason = failed_parent.blocked_reason or (
                        f"Blocked because prerequisite task {failed_parent.id} ended as {failed_parent.status}."
                    )
                    blocked_at = now()
                    result = db.execute(
                        update(Task)
                        .where(Task.id == task.id, Task.status == "queued")
                        .values(
                            status="blocked", blocked_reason=root_reason,
                            finished_at=blocked_at, error_summary=root_reason,
                        )
                    )
                    if result.rowcount != 1:
                        continue
                    task.status = "blocked"
                    task.blocked_reason = root_reason
                    task.finished_at = blocked_at
                    task.error_summary = root_reason
                    db.add(TaskEvent(task_id=task.id, event_type="task.blocked", payload={
                        "status": "blocked", "reason": root_reason,
                        "failed_ancestor_task_id": failed_parent.id, "workflow_id": workflow.id,
                    }))
                    changed = True
            statuses = [task.status for task in tasks]
            target_status = None
            if statuses and all(status == "succeeded" for status in statuses):
                target_status = "succeeded"
            elif any(status in {"failed", "blocked"} for status in statuses):
                target_status = "failed"
            elif any(status == "cancelled" for status in statuses):
                target_status = "cancelled"
            elif any(task.started_at is not None for task in tasks):
                target_status = "running"
            if target_status is not None:
                values = {"status": target_status}
                if target_status in {"succeeded", "failed", "cancelled"}:
                    values["finished_at"] = now()
                if target_status == "running":
                    first_started = min(
                        (task.started_at for task in tasks if task.started_at is not None), default=now()
                    )
                    values["started_at"] = workflow.started_at or first_started
                db.execute(
                    update(Workflow)
                    .where(Workflow.id == workflow.id, Workflow.status.in_(["queued", "running"]))
                    .values(**values)
                )
        db.commit()


def claim_task(worker_id: str = "legacy-worker") -> str | None:
    reconcile_workflows()
    with SessionLocal() as db:
        started_at = now()
        lease_expires_at = started_at + timedelta(seconds=WORKER_LEASE_SECONDS)
        candidates = list(db.scalars(
            select(Task)
            .where(Task.status == "queued", Task.available_at <= started_at)
            .order_by(Task.priority.desc(), Task.created_at.asc(), Task.id.asc())
        ))
        task = None
        for candidate in candidates:
            if candidate.workflow_id is None:
                task = candidate
                break
            parent_ids = db.scalars(select(WorkflowTaskDependency.upstream_task_id).where(
                WorkflowTaskDependency.workflow_id == candidate.workflow_id,
                WorkflowTaskDependency.downstream_task_id == candidate.id,
            ))
            parent_ids = list(parent_ids)
            if not parent_ids:
                task = candidate
                break
            parent_statuses = list(db.scalars(select(Task.status).where(Task.id.in_(parent_ids))))
            if len(parent_statuses) == len(parent_ids) and all(status == "succeeded" for status in parent_statuses):
                task = candidate
                break
        if task is None:
            return None
        attempt_number = int(task.attempt_count or 0) + 1
        workflow_not_cancelled = or_(
            Task.workflow_id.is_(None),
            exists(
                select(Workflow.id).where(
                    Workflow.id == Task.workflow_id,
                    Workflow.status != "cancelled",
                )
            ),
        )
        result = db.execute(
            update(Task)
            .where(Task.id == task.id, Task.status == "queued", workflow_not_cancelled)
            .values(status="running", started_at=started_at, attempt_count=Task.attempt_count + 1, worker_id=worker_id, lease_expires_at=lease_expires_at)
        )
        if result.rowcount != 1:
            db.rollback()
            return None
        task_id = task.id
        if task.workflow_id is not None:
            workflow = db.get(Workflow, task.workflow_id)
            if workflow is not None and workflow.status == "queued":
                workflow.status = "running"
                workflow.started_at = started_at
        db.add(TaskEvent(
            task_id=task_id,
            event_type="executor.claimed",
            payload={
                "queue_wait_ms": elapsed_ms(task.created_at, started_at),
                "queued_at": task.created_at.isoformat(),
                "claimed_at": started_at.isoformat(),
                "priority": task.priority,
                "attempt": attempt_number,
                "max_attempts": task.max_attempts,
            },
        ))
        db.commit()
        return task_id



async def run_repair_attempt(
    task_id: str,
    workspace: Path,
    permission_mode: str,
    repair_prompt: str,
    task_dir: Path,
    repair_number: int,
    max_repair_attempts: int,
    verification_commands: list[str],
) -> tuple[bool, list[dict], str | None]:
    """Run one bounded repair and re-check the original, already-validated commands."""
    stdout_path = task_dir / f"repair-{repair_number:02d}.stdout.jsonl"
    stderr_path = task_dir / f"repair-{repair_number:02d}.stderr.log"
    add_event(task_id, "repair.started", {
        "repair_attempt": repair_number,
        "max_repair_attempts": max_repair_attempts,
    })
    command = build_command(repair_prompt, permission_mode)
    try:
        with stdout_path.open("w", encoding="utf-8") as out, stderr_path.open("w", encoding="utf-8") as err:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=(os.name == "posix"),
            )

            async def drain(stream, target):
                while chunk := await stream.read(4096):
                    target.write(chunk.decode("utf-8", errors="replace"))
                    target.flush()

            readers = [
                asyncio.create_task(drain(proc.stdout, out)),
                asyncio.create_task(drain(proc.stderr, err)),
            ]
            deadline = asyncio.get_running_loop().time() + min(MAX_TASK_SECONDS, 600)
            reason = None
            try:
                while proc.returncode is None:
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=0.25)
                    except asyncio.TimeoutError:
                        with SessionLocal() as db:
                            current = db.get(Task, task_id)
                            cancelled = bool(current and current.cancel_requested)
                        if cancelled:
                            reason = "cancelled"
                            await terminate_process_tree(proc)
                            break
                        if asyncio.get_running_loop().time() >= deadline:
                            reason = "timeout"
                            await terminate_process_tree(proc)
                            break
            finally:
                await asyncio.gather(*readers, return_exceptions=True)
        if reason or proc.returncode != 0:
            status = reason or "agent_failed"
            add_event(task_id, "repair.finished", {
                "repair_attempt": repair_number,
                "status": status,
                "exit_code": proc.returncode,
            })
            return False, [], reason or f"repair agent exited with {proc.returncode}"
    except Exception as exc:
        add_event(task_id, "repair.finished", {
            "repair_attempt": repair_number,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        })
        return False, [], f"{type(exc).__name__}: {exc}"

    results = []
    for index, command_text in enumerate(verification_commands, start=1):
        with SessionLocal() as db:
            current = db.get(Task, task_id)
            if current is None or current.cancel_requested:
                add_event(task_id, "repair.finished", {
                    "repair_attempt": repair_number,
                    "status": "cancelled",
                    "verification": results,
                })
                return False, results, "cancelled"
        try:
            argv = shlex.split(command_text)
            if not argv:
                raise ValueError("command is empty")
            check = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=(os.name == "posix"),
            )
            communicate_task = asyncio.create_task(check.communicate())
            timed_out = False
            cancelled = False
            deadline = asyncio.get_running_loop().time() + 120
            while not communicate_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(communicate_task), timeout=0.25)
                except asyncio.TimeoutError:
                    with SessionLocal() as db:
                        current = db.get(Task, task_id)
                        cancelled = bool(current is None or current.cancel_requested)
                    if cancelled:
                        await terminate_process_tree(check)
                        break
                    if asyncio.get_running_loop().time() >= deadline:
                        timed_out = True
                        await terminate_process_tree(check)
                        break
            out_bytes, err_bytes = await communicate_task
            if cancelled:
                add_event(task_id, "repair.finished", {
                    "repair_attempt": repair_number,
                    "status": "cancelled",
                    "verification": results,
                })
                return False, results, "cancelled"
            results.append({
                "index": index,
                "command": command_text,
                "exit_code": check.returncode,
                "timed_out": timed_out,
                "stdout": out_bytes.decode("utf-8", errors="replace")[-20000:],
                "stderr": err_bytes.decode("utf-8", errors="replace")[-20000:],
            })
            if timed_out or check.returncode != 0:
                break
        except (OSError, ValueError) as exc:
            results.append({
                "index": index,
                "command": command_text,
                "exit_code": None,
                "timed_out": False,
                "stdout": "",
                "stderr": f"{type(exc).__name__}: {exc}",
            })
            break
    passed = (
        bool(verification_commands)
        and len(results) == len(verification_commands)
        and all(item.get("exit_code") == 0 and not item.get("timed_out") for item in results)
    )
    add_event(task_id, "repair.finished", {
        "repair_attempt": repair_number,
        "status": "passed" if passed else "verification_failed",
        "verification": results,
    })
    return passed, results, None


async def _execute_task(task_id: str, worker_id: str) -> None:
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is None or task.status != "running" or task.worker_id != worker_id:
            return
        workspace = Path(task.workspace_path)
        prompt = task.prompt
        permission_mode = task.permission_mode
        acceptance_criteria = task.acceptance_criteria
        verification_commands = list(task.verification_commands or [])
        auto_repair = bool(task.auto_repair)
        max_repair_attempts = min(2, int(task.max_repair_attempts or 0)) if auto_repair else 0
        attempt_number = max(1, int(task.attempt_count or 1))
        max_attempts = task.max_attempts

    task_dir = STORAGE / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = task_dir / f"stdout.attempt-{attempt_number:03d}.jsonl"
    stderr_path = task_dir / f"stderr.attempt-{attempt_number:03d}.log"
    workspace_before = await capture_workspace_snapshot(workspace, [task_dir])
    command = build_command(prompt, permission_mode)
    logged_command = [CLAUDE_BIN, "-p", "<prompt>", "--output-format", "stream-json", "--verbose", "--permission-prompts", "none"]
    if permission_mode == "plan":
        logged_command.extend(["--permission-mode", "plan"])
    add_event(task_id, "executor.started", {
        "command": logged_command,
        "workspace": str(workspace),
        "permission_mode": permission_mode,
        "attempt": attempt_number,
        "max_attempts": max_attempts,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
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

    verification_status = "pending" if verification_commands else "not_configured"
    verification_results = []
    if exit_code == 0 and error_summary is None and verification_commands and terminal_reason is None:
        add_event(task_id, "verification.started", {
            "command_count": len(verification_commands),
            "timeout_seconds_per_command": 120,
            "acceptance_criteria": acceptance_criteria,
        })
        for index, command_text in enumerate(verification_commands, start=1):
            try:
                argv = shlex.split(command_text)
                if not argv:
                    raise ValueError("command is empty")
                proc = await asyncio.create_subprocess_exec(
                    *argv, cwd=str(workspace),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=(os.name == "posix"),
                )
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=120)
                except asyncio.CancelledError:
                    await terminate_process_tree(proc)
                    raise
                except asyncio.TimeoutError:
                    await terminate_process_tree(proc)
                    stdout_bytes, stderr_bytes = await proc.communicate()
                    verification_results.append({
                        "index": index, "command": command_text, "exit_code": proc.returncode,
                        "timed_out": True,
                        "stdout": stdout_bytes.decode("utf-8", errors="replace")[-20000:],
                        "stderr": stderr_bytes.decode("utf-8", errors="replace")[-20000:],
                    })
                    break
                verification_results.append({
                    "index": index, "command": command_text, "exit_code": proc.returncode,
                    "timed_out": False,
                    "stdout": stdout_bytes.decode("utf-8", errors="replace")[-20000:],
                    "stderr": stderr_bytes.decode("utf-8", errors="replace")[-20000:],
                })
                if proc.returncode != 0:
                    break
            except (OSError, ValueError) as exc:
                verification_results.append({
                    "index": index, "command": command_text, "exit_code": None,
                    "timed_out": False, "stdout": "",
                    "stderr": f"{type(exc).__name__}: {exc}",
                })
                break
        passed = len(verification_results) == len(verification_commands) and all(
            result.get("exit_code") == 0 and not result.get("timed_out")
            for result in verification_results
        )
        verification_status = "passed" if passed else "failed"
        verification_payload = {
            "status": verification_status,
            "acceptance_criteria": acceptance_criteria,
            "commands": verification_results,
        }
        try:
            (task_dir / "verification.json").write_text(
                json.dumps(verification_payload, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            verification_payload["artifact_write_error"] = f"{type(exc).__name__}: {exc}"
        add_event(task_id, "verification.finished", verification_payload)
        if not passed:
            terminal_reason = "verification_failed"
            failed = next((item for item in verification_results if item.get("exit_code") != 0 or item.get("timed_out")), {})
            error_summary = (
                f"Verification failed for command: {failed.get('command', 'unknown command')}"
                + (" (timed out after 120 seconds)" if failed.get("timed_out") else "")
                + (f" (exit code {failed.get('exit_code')})" if failed.get("exit_code") is not None else "")
            )

    initial_verification_results = list(verification_results)
    repair_attempt_count = 0
    if verification_status == "failed" and auto_repair and max_repair_attempts > 0 and terminal_reason == "verification_failed":
        for repair_number in range(1, max_repair_attempts + 1):
            with SessionLocal() as db:
                current = db.get(Task, task_id)
                if current is None or current.cancel_requested:
                    terminal_reason = "cancelled"
                    break
            failed_details = json.dumps(verification_results, ensure_ascii=True, indent=2)[-12000:]
            repair_prompt = (
                "A bounded automatic repair was explicitly enabled for this task.\n"
                "Fix only the cause of the failed acceptance checks in the existing workspace. "
                "Do not broaden scope, remove or weaken tests, alter verification commands, "
                "or claim success without making the checks pass. Preserve the original task intent.\n\n"
                f"Original task instructions:\n{prompt[:12000]}\n\n"
                f"Acceptance criteria:\n{acceptance_criteria or '(not provided)'}\n\n"
                f"Failed verification results:\n{failed_details}\n\n"
                "Make the smallest safe correction. Then stop; the control plane will rerun the checks."
            )
            repair_attempt_count = repair_number
            passed, repair_results, repair_error = await run_repair_attempt(
                task_id, workspace, permission_mode, repair_prompt, task_dir,
                repair_number, max_repair_attempts, verification_commands,
            )
            verification_results = repair_results
            if passed:
                verification_status = "passed"
                terminal_reason = None
                error_summary = None
                (task_dir / "verification.json").write_text(json.dumps({
                    "status": "passed", "acceptance_criteria": acceptance_criteria,
                    "commands": verification_results, "repaired": True,
                    "repair_attempt_count": repair_attempt_count,
                }, ensure_ascii=True, indent=2), encoding="utf-8")
                add_event(task_id, "verification.finished", {
                    "status": "passed", "repaired": True,
                    "repair_attempt_count": repair_attempt_count,
                    "commands": verification_results,
                })
                break
            if repair_error:
                error_summary = f"Automatic repair attempt {repair_number} failed: {repair_error}"
            if repair_error == "cancelled":
                terminal_reason = "cancelled"
                verification_status = "not_run"
                try:
                    (task_dir / "verification.json").write_text(json.dumps({
                        "status": "cancelled",
                        "acceptance_criteria": acceptance_criteria,
                        "commands": initial_verification_results,
                        "repair_attempt_count": repair_attempt_count,
                        "repair_cancelled": True,
                    }, ensure_ascii=True, indent=2), encoding="utf-8")
                except OSError:
                    pass
                break
            if repair_error == "timeout":
                terminal_reason = "timeout"
                break
            verification_status = "failed"
            terminal_reason = "verification_failed"
        if verification_status == "failed":
            try:
                (task_dir / "verification.json").write_text(json.dumps({
                    "status": "failed", "acceptance_criteria": acceptance_criteria,
                    "commands": verification_results, "repaired": False,
                    "repair_attempt_count": repair_attempt_count,
                }, ensure_ascii=True, indent=2), encoding="utf-8")
            except OSError:
                pass

    # Preserve compatibility aliases for existing clients while retaining immutable per-attempt logs.
    try:
        shutil.copyfile(stdout_path, task_dir / "stdout.jsonl")
    except OSError:
        pass
    try:
        shutil.copyfile(stderr_path, task_dir / "stderr.log")
    except OSError:
        pass

    workspace_after = await capture_workspace_snapshot(workspace, [task_dir])
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
        if task is None or task.status != "running" or task.worker_id != worker_id:
            return
        task.exit_code, task.session_id = exit_code, session_id
        task.repair_attempt_count = repair_attempt_count
        attempt_error = error_summary or (None if exit_code == 0 else f"Claude Code exited with status {exit_code}")
        retry_scheduled = False
        if task.cancel_requested or terminal_reason == "cancelled":
            task.status = "cancelled"
            task.finished_at = now()
            task.error_summary = attempt_error or "Cancelled by request"
        elif exit_code == 0 and error_summary is None and verification_status != "failed":
            task.status = "succeeded"
            task.finished_at = now()
            task.error_summary = None
        elif task.attempt_count > 0 and task.attempt_count < task.max_attempts and terminal_reason not in {"timeout", "worker_interrupted", "verification_failed"}:
            delay = RETRY_BASE_DELAY_SECONDS * (2 ** max(0, task.attempt_count - 1))
            task.status = "queued"
            task.finished_at = None
            task.available_at = now() + timedelta(seconds=delay)
            task.error_summary = attempt_error
            retry_scheduled = True
            db.add(TaskEvent(task_id=task_id, event_type="executor.retry_scheduled", payload={
                "attempt": task.attempt_count,
                "max_attempts": task.max_attempts,
                "retry_delay_seconds": delay,
                "available_at": task.available_at.isoformat(),
                "error_summary": attempt_error,
            }))
        else:
            task.status = "failed"
            task.finished_at = now()
            task.error_summary = attempt_error or "Task failed"
            if verification_status == "pending":
                verification_status = "not_run"
        task.verification_status = verification_status
        task.worker_id = None
        task.lease_expires_at = None
        db.add(TaskEvent(task_id=task_id, event_type="executor.finished", payload={
            "status": task.status,
            "exit_code": exit_code,
            "session_id": session_id,
            "error_summary": task.error_summary,
            "verification_status": task.verification_status,
            "terminal_reason": "retry_scheduled" if retry_scheduled else (terminal_reason or ("success" if task.status == "succeeded" else "error")),
            "attempt": task.attempt_count,
            "max_attempts": task.max_attempts,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "latest_stdout_alias": str(task_dir / "stdout.jsonl"),
            "latest_stderr_alias": str(task_dir / "stderr.log"),
        }))
        db.commit()
    reconcile_workflows()


def recover_orphaned_running_tasks() -> int:
    """Fail only expired leases; legacy rows receive a lease-length startup grace period."""
    recovered_at = now()
    legacy_cutoff = recovered_at - timedelta(seconds=WORKER_LEASE_SECONDS)
    recovered = 0
    with SessionLocal() as db:
        candidates = list(db.scalars(select(Task).where(
            Task.status == "running",
            or_(
                Task.lease_expires_at <= recovered_at,
                (Task.lease_expires_at.is_(None)) & (Task.started_at <= legacy_cutoff),
            ),
        )))
        for task in candidates:
            previous_started_at = task.started_at.isoformat() if task.started_at else None
            result = db.execute(
                update(Task)
                .where(
                    Task.id == task.id,
                    Task.status == "running",
                    or_(
                        Task.lease_expires_at <= recovered_at,
                        (Task.lease_expires_at.is_(None)) & (Task.started_at <= legacy_cutoff),
                    ),
                )
                .execution_options(synchronize_session=False)
                .values(
                    status="failed", finished_at=recovered_at, exit_code=125,
                    error_summary=(
                        "Worker lease expired. The task was marked failed and was not retried "
                        "automatically because the previous agent process may have produced side effects. "
                        "Inspect workspace and process state before manually retrying."
                    ),
                    worker_id=None, lease_expires_at=None,
                )
            )
            if result.rowcount != 1:
                continue
            recovered += 1
            db.add(TaskEvent(
                task_id=task.id,
                event_type="worker.orphaned_task_recovered",
                payload={
                    "recovered_at": recovered_at.isoformat(),
                    "previous_started_at": previous_started_at,
                    "attempt": task.attempt_count,
                    "max_attempts": task.max_attempts,
                    "automatic_retry": False,
                    "reason": "lease_expired",
                },
            ))
        db.commit()
    return recovered


async def _renew_worker_lease(task_id: str, worker_id: str) -> None:
    """Renew ownership periodically; cancel execution immediately after losing the lease."""
    while True:
        await asyncio.sleep(WORKER_HEARTBEAT_SECONDS)
        expires_at = now() + timedelta(seconds=WORKER_LEASE_SECONDS)
        with SessionLocal() as db:
            result = db.execute(
                update(Task)
                .where(
                    Task.id == task_id,
                    Task.status == "running",
                    Task.worker_id == worker_id,
                )
                .values(lease_expires_at=expires_at)
            )
            db.commit()
        if result.rowcount != 1:
            raise RuntimeError(f"Worker lease lost for task {task_id}")


async def execute_task(task_id: str, worker_id: str | None = None) -> None:
    if worker_id is None:
        worker_id = "direct-execution"
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            if task is None:
                return
            if task.status == "queued":
                task.status = "running"
                task.started_at = now()
            task.worker_id = worker_id
            task.lease_expires_at = now() + timedelta(seconds=WORKER_LEASE_SECONDS)
            db.commit()
    execution = asyncio.create_task(_execute_task(task_id, worker_id))
    heartbeat = asyncio.create_task(_renew_worker_lease(task_id, worker_id))
    try:
        done, _ = await asyncio.wait(
            {execution, heartbeat}, return_when=asyncio.FIRST_COMPLETED
        )
        if heartbeat in done:
            error = heartbeat.exception()
            if error is not None:
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
                return
        await execution
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        if not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)


async def run_worker() -> None:
    worker_id = str(__import__("uuid").uuid4())
    recovered = recover_orphaned_running_tasks()
    reconcile_workflows()
    print(
        f"AI Work OS worker started; id={worker_id}; concurrency={MAX_CONCURRENT_TASKS}; "
        f"lease_seconds={WORKER_LEASE_SECONDS}; heartbeat_seconds={WORKER_HEARTBEAT_SECONDS}; "
        f"recovered_orphaned_tasks={recovered}.",
        flush=True,
    )
    active: set[asyncio.Task] = set()
    while True:
        active = {job for job in active if not job.done()}
        while len(active) < MAX_CONCURRENT_TASKS:
            task_id = claim_task(worker_id)
            if task_id is None:
                break
            active.add(asyncio.create_task(execute_task(task_id, worker_id)))
        if active:
            await asyncio.wait(active, timeout=POLL_SECONDS, return_when=asyncio.FIRST_COMPLETED)
        else:
            await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(run_worker())
