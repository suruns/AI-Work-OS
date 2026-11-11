from __future__ import annotations
import asyncio
import hashlib
import json
import os
import signal
import shutil
import stat
import shlex
import socket
from datetime import datetime, timezone, timedelta
from pathlib import Path
from sqlalchemy import exists, or_, select, update
from .db import Base, SessionLocal, engine, ensure_schema_compatibility
from .models import Task, TaskEvent, Workflow, WorkflowTaskDependency, Worker, elapsed_ms
from .sandbox import build_sandbox_invocation, sandbox_mode

def heartbeat_worker(worker_id: str, concurrency: int | None = None) -> None:
    """Persist process liveness; active task ownership remains authoritative on Task rows."""
    timestamp = now()
    with SessionLocal() as db:
        active_count = db.scalar(
            select(__import__("sqlalchemy").func.count(Task.id)).where(
                Task.worker_id == worker_id, Task.status == "running"
            )
        ) or 0
        record = db.get(Worker, worker_id)
        if record is None:
            record = Worker(
                worker_id=worker_id, hostname=socket.gethostname(), pid=os.getpid(),
                started_at=timestamp, last_heartbeat_at=timestamp,
            )
            db.add(record)
        record.hostname = socket.gethostname()
        record.pid = os.getpid()
        record.last_heartbeat_at = timestamp
        record.status = "busy" if active_count else "idle"
        record.active_task_count = int(active_count)
        if concurrency is not None:
            record.concurrency = concurrency
        db.commit()


def mark_worker_offline(worker_id: str) -> None:
    """Record graceful shutdown; crashed processes are detected by heartbeat expiry."""
    with SessionLocal() as db:
        record = db.get(Worker, worker_id)
        if record is not None:
            record.status = "offline"
            record.active_task_count = int(db.scalar(
                select(__import__("sqlalchemy").func.count(Task.id)).where(
                    Task.worker_id == worker_id, Task.status == "running"
                )
            ) or 0)
            db.commit()



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
CLAUDE_MODEL = os.getenv("AI_WORK_OS_CLAUDE_MODEL", "").strip() or None


def now() -> datetime:
    return datetime.now(timezone.utc)


def claude_setting_sources() -> list[str]:
    """Return the explicitly selected Claude Code settings sources.

    User settings are excluded by default so ~/.claude/settings.json cannot
    silently re-inject model-selection environment variables into Worker runs.
    Operators may opt back into user settings through the environment.
    """
    raw = os.getenv("AI_WORK_OS_CLAUDE_SETTING_SOURCES", "project,local")
    sources = [item.strip() for item in raw.split(",") if item.strip()]
    allowed = {"user", "project", "local"}
    if not sources or len(sources) != len(set(sources)) or set(sources) - allowed:
        raise ValueError(
            "AI_WORK_OS_CLAUDE_SETTING_SOURCES must be a non-empty, comma-separated "
            "subset of user,project,local with no duplicates"
        )
    return sources


def model_override_env_keys(env: dict[str, str]) -> list[str]:
    """List model-selection variables removed from the inherited process environment."""
    return sorted(
        key for key in env
        if key == "ANTHROPIC_MODEL"
        or (key.startswith("ANTHROPIC_DEFAULT_") and key.endswith("_MODEL"))
    )


def claude_subprocess_env() -> dict[str, str]:
    """Remove inherited model overrides while preserving provider/auth environment."""
    child_env = os.environ.copy()
    for key in model_override_env_keys(child_env):
        child_env.pop(key, None)
    child_env.pop("AI_WORK_OS_CLAUDE_MODEL", None)
    child_env.pop("AI_WORK_OS_CLAUDE_SETTING_SOURCES", None)
    return child_env


def write_isolated_mcp_config(task_dir: Path) -> Path:
    """Create an empty MCP config without a permissive first-write window."""
    path = task_dir / "mcp-config.isolated.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            stream.write(json.dumps({"mcpServers": {}}, separators=(",", ":")) + "\n")
    finally:
        if fd >= 0:
            os.close(fd)
    return path


def build_command(
    prompt: str,
    permission_mode: str,
    mcp_config_path: Path = Path("mcp-config.isolated.json"),
    setting_sources: list[str] | None = None,
) -> list[str]:
    sources = setting_sources if setting_sources is not None else claude_setting_sources()
    command = [
        CLAUDE_BIN, "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--permission-prompts", "none",
        "--setting-sources", ",".join(sources),
        "--strict-mcp-config", "--mcp-config", str(mcp_config_path),
        "--disallowedTools", "mcp__*",
    ]
    if CLAUDE_MODEL:
        command.extend(["--model", CLAUDE_MODEL])
    if permission_mode == "plan":
        command.extend(["--permission-mode", "plan"])
    return command


def process_group_exists(pgid: int) -> bool:
    """Return whether a POSIX process group still exists (including unverified access)."""
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


async def terminate_process_tree(
    proc: asyncio.subprocess.Process, grace_seconds: float = 5.0
) -> bool:
    """Terminate the whole process group, even when its leader has already exited.

    Returns True only when POSIX confirms the group no longer exists. On platforms
    without process-group probing, False means termination could not be verified.
    """
    if os.name == "posix":
        pgid = proc.pid  # subprocesses are launched with start_new_session=True
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except (PermissionError, OSError):
            return False

        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
            except asyncio.TimeoutError:
                pass

        deadline = asyncio.get_running_loop().time() + grace_seconds
        while process_group_exists(pgid) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        if process_group_exists(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except (PermissionError, OSError):
                return False
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    return False
            verify_deadline = asyncio.get_running_loop().time() + 1.0
            while process_group_exists(pgid) and asyncio.get_running_loop().time() < verify_deadline:
                await asyncio.sleep(0.05)
        return not process_group_exists(pgid)

    if proc.returncode is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
    return False


async def _git_output(
    workspace: Path, *args: str, task_dir: Path | None = None,
    task_id: str | None = None, sandboxed: bool = True,
) -> bytes:
    git_bin = shutil.which("git", path="/usr/bin:/bin")
    if git_bin is None:
        raise FileNotFoundError("system Git executable not found")
    command = [git_bin, "-C", str(workspace), *args]
    invocation = None
    if sandboxed and sandbox_mode() == "bubblewrap":
        if task_dir is None or task_id is None:
            raise RuntimeError("sandboxed Git snapshot requires task context")
        command = [git_bin, "-c", "core.fsmonitor=false", "-C", str(workspace), *args]
        snapshot_env = {
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
        }
        invocation = build_task_sandbox_invocation(
            task_id, command, workspace, task_dir, "plan", snapshot_env,
            execution_kind="snapshot",
        )
    host_env = None
    if not sandboxed and sandbox_mode() == "bubblewrap":
        host_env = {
            "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    proc = await asyncio.create_subprocess_exec(
        *(invocation.command if invocation else command),
        cwd=invocation.cwd if invocation else None,
        env=invocation.env if invocation else host_env,
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
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        digest.update(b"symlink\0")
        digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
    else:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


async def capture_workspace_snapshot(
    workspace: Path, exclude_paths: list[Path] | None = None, *,
    task_dir: Path | None = None, task_id: str | None = None,
) -> dict:
    """Capture a Git workspace's net diff state without storing source contents."""
    try:
        root = Path((await _git_output(
            workspace, "rev-parse", "--show-toplevel", sandboxed=False
        )).decode().strip()).resolve()
        if sandbox_mode() == "bubblewrap" and root != workspace.resolve():
            return {
                "available": False,
                "reason": "Sandboxed Git snapshots require the selected workspace to be the repository root.",
            }
        git_context = {"task_dir": task_dir, "task_id": task_id}
        tracked_paths = (await _git_output(
            root, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", "HEAD",
            **git_context,
        )).split(b"\0")
        untracked_paths = (await _git_output(
            root, "ls-files", "--others", "--exclude-standard", "-z", **git_context,
        )).split(b"\0")
        def is_generated_python_cache(rel: str) -> bool:
            parts = Path(rel).parts
            return "__pycache__" in parts or rel.endswith(".pyc")

        excluded = [path.resolve() for path in (exclude_paths or [])]

        def is_excluded(candidate: Path) -> bool:
            absolute = candidate.absolute()
            resolved = candidate.resolve(strict=False)
            return any(
                absolute == item or item in absolute.parents
                or resolved == item or item in resolved.parents
                for item in excluded
            )

        tracked = {}
        for raw_path in filter(None, tracked_paths):
            rel = raw_path.decode("utf-8", errors="surrogateescape")
            rel_path = Path(rel)
            if rel_path.is_absolute() or ".." in rel_path.parts:
                continue
            if is_generated_python_cache(rel) or is_excluded(root / rel_path):
                continue
            patch = await _git_output(
                root, "diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD", "--", rel,
                **git_context,
            )
            tracked[rel] = hashlib.sha256(patch).hexdigest()
        untracked = {}
        for raw_path in filter(None, untracked_paths):
            rel = raw_path.decode("utf-8", errors="surrogateescape")
            rel_path = Path(rel)
            if rel_path.is_absolute() or ".." in rel_path.parts:
                continue
            if is_generated_python_cache(rel):
                continue
            candidate = root / rel_path
            resolved_parent = candidate.parent.resolve()
            if not (resolved_parent == root or root in resolved_parent.parents):
                continue
            if candidate.is_symlink():
                resolved_candidate = candidate
            else:
                resolved_candidate = candidate.resolve(strict=False)
                if not (resolved_candidate == root or root in resolved_candidate.parents):
                    continue
            if is_excluded(candidate):
                continue
            try:
                untracked[rel] = _hash_path(candidate)
            except (FileNotFoundError, PermissionError, OSError):
                untracked[rel] = "unreadable"
        return {"available": True, "repository_root": str(root), "tracked": tracked, "untracked": untracked}
    except (FileNotFoundError, RuntimeError, ValueError, asyncio.TimeoutError, OSError) as exc:
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


def build_task_sandbox_invocation(
    task_id: str, command: list[str], workspace: Path, task_dir: Path,
    permission_mode: str, env: dict[str, str] | None, *,
    mcp_config_path: Path | None = None, execution_kind: str,
):
    """Build one task subprocess boundary and persist non-secret policy metadata."""
    try:
        invocation = build_sandbox_invocation(
            command, workspace, task_dir, permission_mode, env,
            mcp_config_path=mcp_config_path, execution_kind=execution_kind,
        )
    except Exception as exc:
        add_event(task_id, "executor.sandbox_failed", {
            "backend": os.getenv("AI_WORK_OS_SANDBOX_MODE", "off"),
            "execution_kind": execution_kind,
            "error": f"{type(exc).__name__}: {exc}",
            "process_started": False,
        })
        raise OSError(f"sandbox setup failed: {type(exc).__name__}: {exc}") from exc
    add_event(task_id, "executor.sandbox_configured", invocation.metadata)
    return invocation


def _blocked_origin(task_id: str, by_id: dict, parents: dict[str, list[str]], memo: dict[str, str | None],
                    visiting: set[str] | None = None) -> str | None:
    """Resolve whether a blocked task ultimately descends from a failed or cancelled task."""
    if task_id in memo:
        return memo[task_id]
    visiting = visiting or set()
    if task_id in visiting:
        return None
    visiting.add(task_id)
    origin: str | None = None
    for parent_id in parents.get(task_id, []):
        parent = by_id.get(parent_id)
        if parent is None:
            continue
        if parent.status == "failed":
            origin = "failed"
            break
        if parent.status == "cancelled":
            if origin is None:
                origin = "cancelled"
            continue
        if parent.status == "blocked":
            parent_origin = _blocked_origin(parent_id, by_id, parents, memo, visiting)
            if parent_origin == "failed":
                origin = "failed"
                break
            if parent_origin == "cancelled" and origin is None:
                origin = "cancelled"
    visiting.discard(task_id)
    memo[task_id] = origin
    return origin


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
                    if failed_parent.status in {"failed", "cancelled"}:
                        ancestor_status = failed_parent.status
                    else:
                        ancestor_status = _blocked_origin(failed_parent.id, by_id, parents, {}) or failed_parent.status
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
                        "failed_ancestor_task_id": failed_parent.id,
                        "failed_ancestor_status": ancestor_status,
                        "workflow_id": workflow.id,
                    }))
                    changed = True
            if not tasks:
                continue
            pending = any(task.status in {"queued", "running"} for task in tasks)
            if pending:
                if any(task.started_at is not None for task in tasks):
                    first_started = min(
                        (task.started_at for task in tasks if task.started_at is not None), default=now()
                    )
                    db.execute(
                        update(Workflow)
                        .where(Workflow.id == workflow.id, Workflow.status == "queued")
                        .values(status="running", started_at=first_started)
                    )
                continue
            statuses = [task.status for task in tasks]
            memo: dict[str, str | None] = {}
            blocked_origins = {
                task.id: _blocked_origin(task.id, by_id, parents, memo)
                for task in tasks if task.status == "blocked"
            }
            has_failure = any(status == "failed" for status in statuses) or any(
                origin == "failed" for origin in blocked_origins.values()
            )
            has_cancellation = any(status == "cancelled" for status in statuses) or any(
                origin == "cancelled" for origin in blocked_origins.values()
            )
            if all(status == "succeeded" for status in statuses):
                target_status = "succeeded"
            elif has_failure:
                target_status = "failed"
            elif has_cancellation:
                target_status = "cancelled"
            else:
                target_status = "failed"
            db.execute(
                update(Workflow)
                .where(Workflow.id == workflow.id, Workflow.status.in_(["queued", "running"]))
                .values(status=target_status, finished_at=now())
            )
        db.commit()


def claim_task(worker_id: str = "legacy-worker") -> str | None:
    if worker_id != "legacy-worker":
        heartbeat_worker(worker_id, MAX_CONCURRENT_TASKS)
    reconcile_workflows()
    with SessionLocal() as db:
        # SQLite's deferred transactions can let two workers read the same queued
        # row and then fail while both try to upgrade their read locks to writes.
        # Acquire the write reservation before reading candidates so claimers
        # serialize; the conditional UPDATE below remains the final status fence.
        if db.bind is not None and db.bind.dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        started_at = now()
        lease_expires_at = started_at + timedelta(seconds=WORKER_LEASE_SECONDS)
        candidates = list(db.scalars(
            select(Task)
            .where(Task.status == "queued", Task.available_at <= started_at)
            .order_by(Task.priority.desc(), Task.created_at.asc(), Task.id.asc())
        ))
        task = None
        attempt_number = 1
        workflow_not_cancelled = or_(
            Task.workflow_id.is_(None),
            exists(
                select(Workflow.id).where(
                    Workflow.id == Task.workflow_id,
                    Workflow.status != "cancelled",
                )
            ),
        )
        for candidate in candidates:
            if candidate.workflow_id is not None:
                parent_ids = list(db.scalars(select(WorkflowTaskDependency.upstream_task_id).where(
                    WorkflowTaskDependency.workflow_id == candidate.workflow_id,
                    WorkflowTaskDependency.downstream_task_id == candidate.id,
                )))
                if parent_ids:
                    parent_statuses = list(db.scalars(select(Task.status).where(Task.id.in_(parent_ids))))
                    if len(parent_statuses) != len(parent_ids) or not all(
                        status == "succeeded" for status in parent_statuses
                    ):
                        continue
            attempt_number = int(candidate.attempt_count or 0) + 1
            result = db.execute(
                update(Task)
                .where(Task.id == candidate.id, Task.status == "queued", workflow_not_cancelled)
                .values(status="running", started_at=started_at, attempt_count=Task.attempt_count + 1, worker_id=worker_id, lease_expires_at=lease_expires_at)
            )
            if result.rowcount != 1:
                # The candidate became unclaimable (for example its workflow was cancelled).
                # Keep scanning instead of returning, so it cannot starve later tasks.
                continue
            task = candidate
            break
        if task is None:
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
        if worker_id != "legacy-worker":
            heartbeat_worker(worker_id, MAX_CONCURRENT_TASKS)
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
    mcp_config_path: Path | None = task_dir / "mcp-config.isolated.json"
    setting_sources: list[str] = []
    repair_started = False
    try:
        try:
            setting_sources = claude_setting_sources()
        except Exception as exc:
            add_event(task_id, "executor.settings_isolation_failed", {
                "policy": "configuration_failed",
                "requested_sources": os.getenv("AI_WORK_OS_CLAUDE_SETTING_SOURCES", "project,local"),
                "execution_kind": "repair",
                "repair_attempt": repair_number,
                "error": f"{type(exc).__name__}: {exc}",
            })
            raise
        mcp_config_path = write_isolated_mcp_config(task_dir)
        add_event(task_id, "repair.started", {
            "repair_attempt": repair_number,
            "max_repair_attempts": max_repair_attempts,
            "settings_policy": "exclude_user" if "user" not in setting_sources else "include_user",
            "setting_sources": setting_sources,
            "model_policy": "explicit_worker_model" if CLAUDE_MODEL else "cli_default",
            "configured_model": CLAUDE_MODEL,
            "mcp_policy": "deny_all",
            "mcp_config_path": str(mcp_config_path),
        })
        repair_started = True
        add_event(task_id, "executor.settings_isolation_configured", {
            "policy": "exclude_user" if "user" not in setting_sources else "include_user",
            "setting_sources": setting_sources,
            "inherited_model_env_keys_removed": model_override_env_keys(os.environ),
            "configured_model": CLAUDE_MODEL,
            "execution_kind": "repair",
            "repair_attempt": repair_number,
        })
        add_event(task_id, "executor.mcp_isolation_configured", {
            "policy": "deny_all",
            "config_path": str(mcp_config_path),
            "configured_server_count": 0,
            "disallowed_tool_pattern": "mcp__*",
            "execution_kind": "repair",
            "repair_attempt": repair_number,
        })
        command = build_command(repair_prompt, permission_mode, mcp_config_path, setting_sources)
        invocation = build_task_sandbox_invocation(
            task_id, command, workspace, task_dir, permission_mode, claude_subprocess_env(),
            mcp_config_path=mcp_config_path, execution_kind="repair",
        )
        with stdout_path.open("w", encoding="utf-8") as out, stderr_path.open("w", encoding="utf-8") as err:
            proc = await asyncio.create_subprocess_exec(
                *invocation.command,
                cwd=invocation.cwd,
                env=invocation.env,
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
            except asyncio.CancelledError:
                await terminate_process_tree(proc)
                raise
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
        error = f"{type(exc).__name__}: {exc}"
        if not repair_started:
            add_event(task_id, "repair.started", {
                "repair_attempt": repair_number,
                "max_repair_attempts": max_repair_attempts,
                "settings_policy": (
                    "configuration_failed" if not setting_sources
                    else ("exclude_user" if "user" not in setting_sources else "include_user")
                ),
                "setting_sources": setting_sources,
                "mcp_policy": "deny_all",
                "mcp_config_path": str(mcp_config_path) if mcp_config_path else None,
            })
            if setting_sources:
                add_event(task_id, "executor.mcp_isolation_failed", {
                    "policy": "deny_all",
                    "config_path": str(mcp_config_path) if mcp_config_path else None,
                    "execution_kind": "repair",
                    "repair_attempt": repair_number,
                    "error": error,
                })
        add_event(task_id, "repair.finished", {
            "repair_attempt": repair_number,
            "status": "error",
            "error": error,
        })
        return False, [], error

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
            invocation = build_task_sandbox_invocation(
                task_id, argv, workspace, task_dir, permission_mode, None,
                execution_kind="verification",
            )
            check = await asyncio.create_subprocess_exec(
                *invocation.command,
                cwd=invocation.cwd, env=invocation.env,
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
                except asyncio.CancelledError:
                    await terminate_process_tree(check)
                    await asyncio.gather(communicate_task, return_exceptions=True)
                    raise
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
    workspace_before = await capture_workspace_snapshot(workspace, [task_dir], task_dir=task_dir, task_id=task_id)
    mcp_config_path: Path | None = None
    mcp_config_attempted = False
    exit_code, error_summary, session_id = None, None, None
    terminal_reason = None
    process_tree_stopped: bool | None = None
    result_event_seen = False
    final_assistant_text_seen = False

    try:
        try:
            setting_sources = claude_setting_sources()
        except Exception as exc:
            add_event(task_id, "executor.settings_isolation_failed", {
                "policy": "configuration_failed",
                "requested_sources": os.getenv("AI_WORK_OS_CLAUDE_SETTING_SOURCES", "project,local"),
                "execution_kind": "task",
                "error": f"{type(exc).__name__}: {exc}",
            })
            raise
        mcp_config_attempted = True
        mcp_config_path = write_isolated_mcp_config(task_dir)
        command = build_command(prompt, permission_mode, mcp_config_path, setting_sources)
        invocation = build_task_sandbox_invocation(
            task_id, command, workspace, task_dir, permission_mode, claude_subprocess_env(),
            mcp_config_path=mcp_config_path, execution_kind="task",
        )
        logged_command = [
            CLAUDE_BIN, "-p", "<prompt>",
            "--output-format", "stream-json", "--verbose",
            "--permission-prompts", "none",
            "--setting-sources", ",".join(setting_sources),
            "--strict-mcp-config", "--mcp-config", "<isolated-mcp-config>",
            "--disallowedTools", "mcp__*",
        ]
        if CLAUDE_MODEL:
            logged_command.extend(["--model", CLAUDE_MODEL])
        if permission_mode == "plan":
            logged_command.extend(["--permission-mode", "plan"])
        add_event(task_id, "executor.started", {
            "command": logged_command,
            "workspace": str(workspace),
            "permission_mode": permission_mode,
            "settings_policy": "exclude_user" if "user" not in setting_sources else "include_user",
            "setting_sources": setting_sources,
            "model_env_policy": "strip_inherited_model_overrides",
            "model_policy": "explicit_worker_model" if CLAUDE_MODEL else "cli_default",
            "configured_model": CLAUDE_MODEL,
            "mcp_policy": "deny_all",
            "mcp_config_path": str(mcp_config_path),
            "attempt": attempt_number,
            "max_attempts": max_attempts,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        })
        add_event(task_id, "executor.settings_isolation_configured", {
            "policy": "exclude_user" if "user" not in setting_sources else "include_user",
            "setting_sources": setting_sources,
            "inherited_model_env_keys_removed": model_override_env_keys(os.environ),
            "configured_model": CLAUDE_MODEL,
            "execution_kind": "task",
            "attempt": attempt_number,
        })
        add_event(task_id, "executor.mcp_isolation_configured", {
            "policy": "deny_all",
            "config_path": str(mcp_config_path),
            "configured_server_count": 0,
            "disallowed_tool_pattern": "mcp__*",
        })
        with stdout_path.open("w", encoding="utf-8") as out, stderr_path.open("w", encoding="utf-8") as err:
            proc = await asyncio.create_subprocess_exec(
                *invocation.command,
                cwd=invocation.cwd,
                env=invocation.env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=(os.name == "posix"),
            )

            async def read_stdout() -> None:
                nonlocal session_id, result_event_seen, final_assistant_text_seen
                assert proc.stdout is not None
                while line := await proc.stdout.readline():
                    decoded = line.decode("utf-8", errors="replace")
                    out.write(decoded)
                    out.flush()
                    try:
                        event = json.loads(decoded)
                        event_type = str(event.get("type", "unknown"))
                        if event_type == "result" and not bool(event.get("is_error", False)):
                            # A result event is the normal terminal envelope. If the
                            # CLI includes an explicit stop reason, only natural turn
                            # completion is accepted; tool-use handoffs are not final.
                            stop_reason = event.get("stop_reason")
                            if stop_reason is None:
                                stop_reason = (event.get("message") or {}).get("stop_reason")
                            if stop_reason in (None, "end_turn"):
                                result_event_seen = True
                        if event_type == "assistant":
                            message = event.get("message") or {}
                            content = message.get("content") or []
                            has_text = any(
                                isinstance(block, dict) and block.get("type") == "text"
                                and bool(str(block.get("text", "")).strip())
                                for block in content
                            )
                            if message.get("stop_reason") == "end_turn" and has_text:
                                final_assistant_text_seen = True
                        observed_session_id = event.get("session_id")
                        if observed_session_id:
                            observed_session_id = str(observed_session_id)
                            if session_id is None:
                                session_id = observed_session_id
                                with SessionLocal() as db:
                                    current = db.get(Task, task_id)
                                    if current is not None and current.session_id != observed_session_id:
                                        current.session_id = observed_session_id
                                        db.add(TaskEvent(
                                            task_id=task_id,
                                            event_type="agent.session.started",
                                            payload={
                                                "session_id": observed_session_id,
                                                "worker_id": worker_id,
                                                "attempt": attempt_number,
                                                "source_event_type": event_type,
                                            },
                                        ))
                                        db.commit()
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
                            process_tree_stopped = await terminate_process_tree(proc)
                            terminal_reason = "cancelled"
                            error_summary = "Cancelled by request"
                            add_event(task_id, "executor.cancelled", {"reason": "cancel_requested"})
                            break
                        if asyncio.get_running_loop().time() >= deadline:
                            process_tree_stopped = await terminate_process_tree(proc)
                            terminal_reason = "timeout"
                            error_summary = f"Task timed out after {MAX_TASK_SECONDS} seconds"
                            add_event(task_id, "executor.timed_out", {
                                "timeout_seconds": MAX_TASK_SECONDS,
                                "exit_code": proc.returncode,
                            })
                            break
                # A cancellation can race with the leader's natural exit. Check
                # the persisted request after the wait loop and still clean up
                # descendants in the process group before finalizing the task.
                with SessionLocal() as db:
                    current = db.get(Task, task_id)
                    cancel_requested_after_exit = bool(current and current.cancel_requested)
                if cancel_requested_after_exit and terminal_reason is None:
                    process_tree_stopped = await terminate_process_tree(proc)
                    terminal_reason = "cancelled"
                    error_summary = "Cancelled by request"
                    add_event(task_id, "executor.cancelled", {"reason": "cancel_requested"})
            except asyncio.CancelledError:
                process_tree_stopped = await terminate_process_tree(proc)
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
        if mcp_config_attempted and mcp_config_path is None:
            add_event(task_id, "executor.mcp_isolation_failed", {
                "policy": "deny_all",
                "error": error_summary,
            })

    output_complete = result_event_seen or final_assistant_text_seen
    completion_signal = "result" if result_event_seen else (
        "final_assistant_text" if final_assistant_text_seen else None
    )
    if exit_code == 0 and error_summary is None and terminal_reason is None and not output_complete:
        terminal_reason = "incomplete_output"
        error_summary = (
            "Claude Code exited with status 0 without a terminal result event "
            "or a final assistant message."
        )
    add_event(task_id, "executor.output_validated", {
        "complete": output_complete,
        "completion_signal": completion_signal,
        "result_event_seen": result_event_seen,
        "final_assistant_text_seen": final_assistant_text_seen,
        "exit_code": exit_code,
    })

    if session_id is not None:
        add_event(task_id, "agent.session.ended", {
            "session_id": session_id,
            "worker_id": worker_id,
            "attempt": attempt_number,
            "exit_code": exit_code,
            "terminal_reason": terminal_reason,
            "status": "cancelled" if terminal_reason == "cancelled" else (
                "timed_out" if terminal_reason == "timeout" else (
                    "failed" if exit_code not in (0, None) or error_summary or not output_complete else "completed"
                )
            ),
        })

    verification_status = "pending" if verification_commands else "not_configured"
    verification_results = []
    if exit_code == 0 and error_summary is None and verification_commands and terminal_reason is None:
        add_event(task_id, "verification.started", {
            "command_count": len(verification_commands),
            "timeout_seconds_per_command": 120,
            "acceptance_criteria": acceptance_criteria,
        })
        verification_cancelled = False
        for index, command_text in enumerate(verification_commands, start=1):
            try:
                argv = shlex.split(command_text)
                if not argv:
                    raise ValueError("command is empty")
                invocation = build_task_sandbox_invocation(
                    task_id, argv, workspace, task_dir, permission_mode, None,
                    execution_kind="verification",
                )
                proc = await asyncio.create_subprocess_exec(
                    *invocation.command, cwd=invocation.cwd, env=invocation.env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=(os.name == "posix"),
                )
                communicate_task = asyncio.create_task(proc.communicate())
                timed_out = False
                cancelled = False
                deadline = asyncio.get_running_loop().time() + 120
                while not communicate_task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(communicate_task), timeout=0.25)
                    except asyncio.CancelledError:
                        await terminate_process_tree(proc)
                        await asyncio.gather(communicate_task, return_exceptions=True)
                        raise
                    except asyncio.TimeoutError:
                        with SessionLocal() as db:
                            current = db.get(Task, task_id)
                            cancelled = bool(current is None or current.cancel_requested)
                        if cancelled:
                            await terminate_process_tree(proc)
                            break
                        if asyncio.get_running_loop().time() >= deadline:
                            timed_out = True
                            await terminate_process_tree(proc)
                            break
                stdout_bytes, stderr_bytes = await communicate_task
                verification_results.append({
                    "index": index, "command": command_text, "exit_code": proc.returncode,
                    "timed_out": timed_out, "cancelled": cancelled,
                    "stdout": stdout_bytes.decode("utf-8", errors="replace")[-20000:],
                    "stderr": stderr_bytes.decode("utf-8", errors="replace")[-20000:],
                })
                if cancelled:
                    verification_cancelled = True
                    terminal_reason = "cancelled"
                    break
                if timed_out or proc.returncode != 0:
                    break
            except (OSError, ValueError) as exc:
                verification_results.append({
                    "index": index, "command": command_text, "exit_code": None,
                    "timed_out": False, "stdout": "",
                    "stderr": f"{type(exc).__name__}: {exc}",
                })
                break
        if verification_cancelled:
            verification_status = "not_run"
            verification_payload = {
                "status": "cancelled",
                "acceptance_criteria": acceptance_criteria,
                "commands": verification_results,
            }
        else:
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
            if not passed:
                terminal_reason = "verification_failed"
                failed = next((item for item in verification_results if item.get("exit_code") != 0 or item.get("timed_out")), {})
                error_summary = (
                    f"Verification failed for command: {failed.get('command', 'unknown command')}"
                    + (" (timed out after 120 seconds)" if failed.get("timed_out") else "")
                    + (f" (exit code {failed.get('exit_code')})" if failed.get("exit_code") is not None else "")
                )
        try:
            (task_dir / "verification.json").write_text(
                json.dumps(verification_payload, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            verification_payload["artifact_write_error"] = f"{type(exc).__name__}: {exc}"
        add_event(task_id, "verification.finished", verification_payload)

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

    workspace_after = await capture_workspace_snapshot(workspace, [task_dir], task_dir=task_dir, task_id=task_id)
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
        elif task.attempt_count > 0 and task.attempt_count < task.max_attempts and terminal_reason not in {"timeout", "worker_interrupted", "verification_failed", "incomplete_output"}:
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
        if task.cancel_requested or terminal_reason == "cancelled":
            db.add(TaskEvent(task_id=task_id, event_type="agent.control.acknowledged", payload={
                "action": "cancel",
                "status": "acknowledged",
                "acknowledged_by": worker_id,
                "session_id": session_id,
                "process_tree_stopped": process_tree_stopped is True,
                "process_tree_status": "stopped" if process_tree_stopped is True else "not_verified",
                "note": "The worker completed its cancellation path; process-tree termination is reported as stopped only when the process group was independently verified absent.",
            }))
        db.add(TaskEvent(task_id=task_id, event_type="executor.finished", payload={
            "status": task.status,
            "exit_code": exit_code,
            "session_id": session_id,
            "error_summary": task.error_summary,
            "verification_status": task.verification_status,
            "output_complete": output_complete,
            "completion_signal": completion_signal,
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
        candidates = list(db.scalars(select(Task).where(Task.status == "running")))
        for task in candidates:
            lease = task.lease_expires_at
            started = task.started_at
            if lease is not None:
                if lease.tzinfo is None:
                    lease = lease.replace(tzinfo=timezone.utc)
                expired = lease <= recovered_at
            else:
                if started is not None and started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                expired = started is None or started <= legacy_cutoff
            if not expired:
                continue
            previous_started_at = task.started_at.isoformat() if task.started_at else None
            owner = task.worker_id
            original_lease = task.lease_expires_at
            conditions = [
                Task.id == task.id,
                Task.status == "running",
                Task.worker_id == owner if owner is not None else Task.worker_id.is_(None),
                Task.lease_expires_at == original_lease if original_lease is not None else Task.lease_expires_at.is_(None),
            ]
            result = db.execute(
                update(Task)
                .where(*conditions)
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
        renewed_at = now()
        expires_at = renewed_at + timedelta(seconds=WORKER_LEASE_SECONDS)
        with SessionLocal() as db:
            result = db.execute(
                update(Task)
                .where(
                    Task.id == task_id,
                    Task.status == "running",
                    Task.worker_id == worker_id,
                    Task.lease_expires_at > renewed_at,
                )
                .values(lease_expires_at=expires_at)
            )
            db.commit()
        if result.rowcount != 1:
            raise RuntimeError(f"Worker lease lost for task {task_id}")
        heartbeat_worker(worker_id, MAX_CONCURRENT_TASKS)


async def execute_task(task_id: str, worker_id: str | None = None) -> None:
    if worker_id is None:
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            if task is None:
                return
            # Compatibility path for tests/embedded callers: never steal a live
            # lease from a named worker. claim_task() without an ID uses this
            # stable legacy identity.
            if task.status == "queued":
                worker_id = "direct-execution"
                claimed_at = now()
                result = db.execute(
                    update(Task)
                    .where(Task.id == task_id, Task.status == "queued")
                    .values(
                        status="running",
                        started_at=claimed_at,
                        attempt_count=Task.attempt_count + 1,
                        worker_id=worker_id,
                        lease_expires_at=claimed_at + timedelta(seconds=WORKER_LEASE_SECONDS),
                    )
                )
                if result.rowcount != 1:
                    db.rollback()
                    return
            elif task.status == "running" and task.worker_id == "legacy-worker":
                worker_id = "legacy-worker"
            elif task.status == "running" and task.worker_id is None:
                worker_id = "direct-execution"
                result = db.execute(
                    update(Task)
                    .where(Task.id == task_id, Task.status == "running", Task.worker_id.is_(None))
                    .values(
                        worker_id=worker_id,
                        lease_expires_at=now() + timedelta(seconds=WORKER_LEASE_SECONDS),
                    )
                )
                if result.rowcount != 1:
                    db.rollback()
                    return
            else:
                return
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
    except asyncio.CancelledError:
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
        return
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
    heartbeat_worker(worker_id, MAX_CONCURRENT_TASKS)
    print(
        f"AI Work OS worker started; id={worker_id}; concurrency={MAX_CONCURRENT_TASKS}; "
        f"lease_seconds={WORKER_LEASE_SECONDS}; heartbeat_seconds={WORKER_HEARTBEAT_SECONDS}; "
        f"recovered_orphaned_tasks={recovered}.",
        flush=True,
    )
    active: set[asyncio.Task] = set()
    try:
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
    finally:
        # Stop in-flight executions before publishing the graceful offline state.
        for job in active:
            job.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        mark_worker_offline(worker_id)


if __name__ == "__main__":
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        # SIGINT is an expected graceful shutdown path, not a worker failure.
        pass
