import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from fastapi.responses import FileResponse
from fastapi import FastAPI, HTTPException, Query
from sqlalchemy import func, select, update
from .db import Base, SessionLocal, engine, ensure_schema_compatibility
from .models import Task, TaskEvent, Workflow, WorkflowTaskDependency, utcnow
from .schemas import TaskCreate, TaskRead, TaskEventRead, WorkflowCreate

Base.metadata.create_all(bind=engine)
ensure_schema_compatibility()
app = FastAPI(title="AI Work OS API", version="0.2.0")

WEB_DIR = Path(__file__).resolve().parent / "web"
STORAGE = Path(os.getenv("AI_WORK_OS_STORAGE", "./storage")).resolve()


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/stats")
def task_stats() -> dict:
    with SessionLocal() as db:
        rows = db.execute(select(Task.status, __import__("sqlalchemy").func.count()).group_by(Task.status)).all()
        counts = {status: count for status, count in rows}
        return {"total": sum(counts.values()), "by_status": counts}


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "ai-work-os-api"}


@app.post("/tasks", response_model=TaskRead, status_code=201)
def create_task(body: TaskCreate) -> Task:
    workspace = Path(body.workspace_path).expanduser().resolve()
    if not workspace.is_dir():
        raise HTTPException(status_code=422, detail="workspace_path must be an existing directory")
    task = Task(
        title=body.title.strip(), prompt=body.prompt, workspace_path=str(workspace),
        permission_mode=body.permission_mode, priority=body.priority,
        max_attempts=body.max_attempts,
        acceptance_criteria=body.acceptance_criteria,
        verification_commands=body.verification_commands,
        auto_repair=body.auto_repair,
        max_repair_attempts=body.max_repair_attempts if body.auto_repair else 0,
        verification_status="pending" if body.verification_commands else "not_configured",
    )
    with SessionLocal() as db:
        db.add(task)
        db.commit()
        db.refresh(task)
        db.add(TaskEvent(task_id=task.id, event_type="task.queued", payload={"status": "queued"}))
        db.commit()
        db.refresh(task)
        return task


@app.get("/tasks", response_model=list[TaskRead])
def list_tasks(limit: int = Query(default=50, ge=1, le=200)) -> list[Task]:
    with SessionLocal() as db:
        return list(db.scalars(select(Task).order_by(Task.created_at.desc()).limit(limit)))


@app.get("/tasks/{task_id}", response_model=TaskRead)
def get_task(task_id: str) -> Task:
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        return task


@app.get("/tasks/{task_id}/events", response_model=list[TaskEventRead])
def get_task_events(
    task_id: str,
    include_thinking_tokens: bool = Query(default=True),
) -> list[TaskEvent | dict]:
    thinking_filter = (
        (TaskEvent.event_type == "claude.system")
        & (TaskEvent.payload["subtype"].as_string() == "thinking_tokens")
    )
    with SessionLocal() as db:
        if not db.get(Task, task_id):
            raise HTTPException(status_code=404, detail="task not found")
        base_query = select(TaskEvent).where(TaskEvent.task_id == task_id)
        if include_thinking_tokens:
            return list(db.scalars(base_query.order_by(TaskEvent.id)))

        # Exclude noisy token updates in SQL rather than loading every raw event.
        visible = list(db.scalars(
            base_query.where(~thinking_filter).order_by(TaskEvent.id)
        ))
        token_query = select(TaskEvent).where(
            TaskEvent.task_id == task_id, thinking_filter
        )
        sample_count = db.scalar(
            select(func.count(TaskEvent.id)).where(
                TaskEvent.task_id == task_id, thinking_filter
            )
        ) or 0
        if sample_count:
            first = db.scalar(token_query.order_by(TaskEvent.id.asc()).limit(1))
            latest = db.scalar(token_query.order_by(TaskEvent.id.desc()).limit(1))
            assert first is not None and latest is not None
            visible.append({
                "id": latest.id,
                "task_id": latest.task_id,
                "occurred_at": latest.occurred_at,
                "event_type": "claude.system.thinking_tokens_summary",
                "payload": {
                    "sample_count": sample_count,
                    "first_seen": first.occurred_at.isoformat(),
                    "last_seen": latest.occurred_at.isoformat(),
                    "latest_estimated_tokens": latest.payload.get("estimated_tokens"),
                },
            })
    visible.sort(key=lambda event: event.id if isinstance(event, TaskEvent) else event["id"])
    return visible

@app.get("/tasks/{task_id}/workspace-changes")
def get_workspace_changes(task_id: str) -> dict:
    with SessionLocal() as db:
        if not db.get(Task, task_id):
            raise HTTPException(status_code=404, detail="task not found")
    report_path = STORAGE / "tasks" / task_id / "workspace_changes.json"
    try:
        return json.loads(report_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"available": False, "reason": "Workspace change report is not available yet."}
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not read workspace change report: {type(exc).__name__}") from exc


@app.get("/tasks/{task_id}/workspace-diff")
def get_workspace_diff(task_id: str, path: str = Query(min_length=1, max_length=1000)) -> dict:
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        workspace = Path(task.workspace_path).resolve()

    report_path = STORAGE / "tasks" / task_id / "workspace_changes.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HTTPException(status_code=409, detail="Workspace change report is not available yet.")
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not read workspace change report: {type(exc).__name__}") from exc
    if not report.get("available"):
        raise HTTPException(status_code=409, detail=report.get("reason", "Workspace diff is unavailable."))

    rel_path = PurePosixPath(path)
    if rel_path.is_absolute() or ".." in rel_path.parts or not rel_path.parts or "\x00" in path:
        raise HTTPException(status_code=400, detail="path must be a safe relative workspace path")
    allowed_groups = {
        "changed_during_task": report.get("changed_during_task", []),
        "preexisting_changes": report.get("preexisting_changes", []),
        "final_changes": report.get("final_changes", []),
        "changes_reverted_or_removed": report.get("changes_reverted_or_removed", []),
    }
    if not any(path in values for values in allowed_groups.values()):
        raise HTTPException(status_code=404, detail="path is not included in this task's workspace report")

    try:
        root_result = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
            capture_output=True, timeout=10, check=True,
        )
        root = Path(os.fsdecode(root_result.stdout).strip()).resolve()
        lexical_candidate = root / Path(*rel_path.parts)
        if lexical_candidate.is_symlink():
            raise HTTPException(status_code=400, detail="symlink diffs are not supported")
        candidate = lexical_candidate.resolve(strict=False)
        if candidate != root and root not in candidate.parents:
            raise HTTPException(status_code=400, detail="path resolves outside the Git workspace")
        tracked_check = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", path],
            capture_output=True, timeout=10,
        )
        if tracked_check.returncode == 0:
            diff_result = subprocess.run(
                ["git", "-C", str(root), "diff", "--no-ext-diff", "--unified=4", "HEAD", "--", path],
                capture_output=True, timeout=10,
            )
            diff_bytes = diff_result.stdout
        elif candidate.is_file():
            diff_result = subprocess.run(
                ["git", "-C", str(root), "diff", "--no-index", "--no-ext-diff", "--unified=4", "--", os.devnull, str(candidate)],
                capture_output=True, timeout=10,
            )
            # git diff --no-index returns 1 when differences exist.
            if diff_result.returncode not in (0, 1):
                raise RuntimeError("git could not generate the file diff")
            diff_bytes = diff_result.stdout
        else:
            diff_bytes = b""
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Git diff timed out.") from exc
    except subprocess.CalledProcessError as exc:
        raise HTTPException(status_code=422, detail="Workspace is not a readable Git repository.") from exc
    except OSError as exc:
        raise HTTPException(status_code=422, detail="Could not execute Git to read this diff.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    binary = b"\x00" in diff_bytes
    diff_text = "" if binary else diff_bytes.decode("utf-8", errors="replace")
    truncated = len(diff_text) > 200_000
    if truncated:
        diff_text = diff_text[:200_000] + "\n… diff truncated at 200,000 characters …"
    categories = [name for name, values in allowed_groups.items() if path in values]
    return {
        "path": path,
        "categories": categories,
        "diff": diff_text,
        "binary": binary,
        "truncated": truncated,
        "note": "Diff is the current workspace compared with HEAD; it can include edits that existed before the task.",
    }


@app.get("/tasks/{task_id}/artifacts")
def list_task_artifacts(task_id: str) -> dict:
    """List task-owned logs/reports and workspace files changed during execution."""
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        workspace = Path(task.workspace_path).resolve()

    task_storage_root = STORAGE / "tasks"
    task_dir = task_storage_root / task_id
    artifacts = []
    storage_path_safe = not task_storage_root.is_symlink() and not task_dir.is_symlink()
    for name, kind in (
        ("stdout.jsonl", "execution_log"),
        ("stderr.log", "error_log"),
        ("workspace_changes.json", "workspace_change_report"),
        ("verification.json", "verification_report"),
    ):
        candidate = task_dir / name
        if not storage_path_safe or candidate.is_symlink() or not candidate.is_file():
            continue
        artifacts.append({
            "name": name,
            "kind": kind,
            "size_bytes": candidate.stat().st_size,
            "download_url": f"/tasks/{task_id}/artifacts/{name}",
        })

    if storage_path_safe and task_dir.is_dir():
        for candidate in sorted(task_dir.iterdir()):
            match = re.fullmatch(r"(stdout|stderr)\.attempt-(\d{3})\.(jsonl|log)", candidate.name)
            repair_match = re.fullmatch(r"repair-(\d{2})\.(stdout\.jsonl|stderr\.log)", candidate.name)
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if match:
                kind = "execution_log" if match.group(1) == "stdout" else "error_log"
                attempt = int(match.group(2))
            elif repair_match:
                kind = "repair_execution_log" if repair_match.group(2).startswith("stdout") else "repair_error_log"
                attempt = int(repair_match.group(1))
            else:
                continue
            artifacts.append({
                "name": candidate.name,
                "kind": kind,
                "attempt": attempt,
                "size_bytes": candidate.stat().st_size,
                "download_url": f"/tasks/{task_id}/artifacts/{candidate.name}",
            })

    report_path = task_dir / "workspace_changes.json"
    workspace_files = []
    report_available = False
    try:
        if not storage_path_safe or report_path.is_symlink():
            raise OSError("task artifact storage path is not safe")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report_available = bool(report.get("available"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        report = {}

    if report_available:
        try:
            root_result = subprocess.run(
                ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
                capture_output=True, timeout=10, check=True,
            )
            root = Path(os.fsdecode(root_result.stdout).strip()).resolve()
            for rel in sorted(set(report.get("changed_during_task", []))):
                rel_path = PurePosixPath(rel)
                if rel_path.is_absolute() or ".." in rel_path.parts or not rel_path.parts or "\x00" in rel:
                    continue
                lexical = root / Path(*rel_path.parts)
                # Refuse symlinks anywhere in the path, not only at the leaf.
                chain = [lexical, *lexical.parents]
                if any(item.is_symlink() for item in chain if item == root or root in item.parents):
                    continue
                try:
                    candidate = lexical.resolve(strict=True)
                except (FileNotFoundError, OSError):
                    continue
                if candidate == root or root not in candidate.parents or not candidate.is_file():
                    continue
                workspace_files.append({
                    "path": rel,
                    "size_bytes": candidate.stat().st_size,
                    "download_url": f"/tasks/{task_id}/workspace-file?path={quote(rel, safe='/')}",
                })
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            # Artifact logs remain available even if the workspace can no longer be inspected.
            pass

    return {
        "task_id": task_id,
        "artifacts": artifacts,
        "workspace_files": workspace_files,
        "workspace_files_available": report_available,
    }


@app.get("/tasks/{task_id}/artifacts/{name}")
def download_task_artifact(task_id: str, name: str):
    allowed = {
        "stdout.jsonl": "application/x-ndjson",
        "stderr.log": "text/plain; charset=utf-8",
        "workspace_changes.json": "application/json",
        "verification.json": "application/json",
    }
    if name not in allowed:
        match = re.fullmatch(r"(stdout|stderr)\.attempt-(\d{3})\.(jsonl|log)", name)
        repair_match = re.fullmatch(r"repair-(\d{2})\.(stdout\.jsonl|stderr\.log)", name)
        if match:
            if (match.group(1) == "stdout" and match.group(3) != "jsonl") or (match.group(1) == "stderr" and match.group(3) != "log"):
                raise HTTPException(status_code=404, detail="artifact not found")
            allowed[name] = "application/x-ndjson" if match.group(1) == "stdout" else "text/plain; charset=utf-8"
        elif repair_match:
            allowed[name] = "application/x-ndjson" if repair_match.group(2).startswith("stdout") else "text/plain; charset=utf-8"
        else:
            raise HTTPException(status_code=404, detail="artifact not found")
    with SessionLocal() as db:
        if not db.get(Task, task_id):
            raise HTTPException(status_code=404, detail="task not found")
    task_storage_root = STORAGE / "tasks"
    task_dir = task_storage_root / task_id
    candidate = task_dir / name
    if task_storage_root.is_symlink() or task_dir.is_symlink() or candidate.is_symlink() or not candidate.is_file() or task_dir.resolve() not in candidate.resolve().parents:
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(candidate, media_type=allowed[name], filename=name)


@app.get("/tasks/{task_id}/workspace-file")
def download_workspace_file(task_id: str, path: str = Query(min_length=1, max_length=1000)):
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        workspace = Path(task.workspace_path).resolve()

    rel_path = PurePosixPath(path)
    if rel_path.is_absolute() or ".." in rel_path.parts or not rel_path.parts or "\x00" in path:
        raise HTTPException(status_code=400, detail="path must be a safe relative workspace path")

    task_storage_root = STORAGE / "tasks"
    task_dir = task_storage_root / task_id
    report_path = task_dir / "workspace_changes.json"
    try:
        if task_storage_root.is_symlink() or task_dir.is_symlink() or report_path.is_symlink():
            raise OSError("task artifact storage path is not safe")
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail="Workspace change report is not available yet.") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="Could not read workspace change report.") from exc
    if not report.get("available"):
        raise HTTPException(status_code=409, detail=report.get("reason", "Workspace file downloads are unavailable."))
    if path not in report.get("changed_during_task", []):
        raise HTTPException(status_code=404, detail="path was not changed during this task")

    try:
        root_result = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
            capture_output=True, timeout=10, check=True,
        )
        root = Path(os.fsdecode(root_result.stdout).strip()).resolve()
        lexical = root / Path(*rel_path.parts)
        chain = [lexical, *lexical.parents]
        if any(item.is_symlink() for item in chain if item == root or root in item.parents):
            raise HTTPException(status_code=400, detail="symlink downloads are not supported")
        candidate = lexical.resolve(strict=True)
        if candidate == root or root not in candidate.parents or not candidate.is_file():
            raise HTTPException(status_code=404, detail="workspace file not found")
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Git workspace lookup timed out.") from exc
    except subprocess.CalledProcessError as exc:
        raise HTTPException(status_code=422, detail="Workspace is not a readable Git repository.") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="workspace file not found") from exc
    except OSError as exc:
        raise HTTPException(status_code=422, detail="Could not inspect workspace file.") from exc
    return FileResponse(candidate, media_type="application/octet-stream", filename=candidate.name)


@app.post("/tasks/{task_id}/cancel", response_model=TaskRead)
def cancel_task(task_id: str) -> Task:
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        if task.status in {"queued", "running"}:
            timestamp = utcnow()
            queued_result = db.execute(
                update(Task)
                .where(Task.id == task_id, Task.status == "queued")
                .values(
                    status="cancelled", cancel_requested=1, finished_at=timestamp,
                    error_summary="Cancelled before task execution",
                )
            )
            if queued_result.rowcount == 1:
                db.add(TaskEvent(
                    task_id=task_id, event_type="task.cancelled",
                    payload={"status": "cancelled", "cancelled_before_start": True},
                ))
            else:
                running_result = db.execute(
                    update(Task)
                    .where(Task.id == task_id, Task.status == "running", Task.cancel_requested == 0)
                    .values(cancel_requested=1)
                )
                if running_result.rowcount == 1:
                    db.add(TaskEvent(
                        task_id=task_id, event_type="task.cancel_requested",
                        payload={"status": "running"},
                    ))
            db.commit()
            db.refresh(task)
        return task


def _workflow_counts(db, workflow_id: str) -> dict[str, int]:
    rows = db.execute(
        select(Task.status, func.count()).where(Task.workflow_id == workflow_id).group_by(Task.status)
    ).all()
    return {status: count for status, count in rows}


def _workflow_payload(db, workflow: Workflow) -> dict:
    counts = _workflow_counts(db, workflow.id)
    return {
        "id": workflow.id, "title": workflow.title, "status": workflow.status,
        "created_at": workflow.created_at, "started_at": workflow.started_at,
        "finished_at": workflow.finished_at, "counts": counts,
    }


def _validate_dag(items: list) -> dict[str, list[str]]:
    by_key = {item.key: item for item in items}
    if len(by_key) != len(items):
        raise HTTPException(status_code=422, detail="task keys must be unique")
    graph = {}
    for item in items:
        if item.key in item.depends_on:
            raise HTTPException(status_code=422, detail=f"task {item.key!r} cannot depend on itself")
        unknown = sorted(set(item.depends_on) - set(by_key))
        if unknown:
            raise HTTPException(status_code=422, detail=f"task {item.key!r} references unknown dependencies: {unknown}")
        if len(set(item.depends_on)) != len(item.depends_on):
            raise HTTPException(status_code=422, detail=f"task {item.key!r} has duplicate dependencies")
        graph[item.key] = list(item.depends_on)
    visiting, visited = set(), set()
    def visit(key: str) -> None:
        if key in visiting:
            raise HTTPException(status_code=422, detail="workflow dependencies contain a cycle")
        if key in visited:
            return
        visiting.add(key)
        for parent in graph[key]:
            visit(parent)
        visiting.remove(key)
        visited.add(key)
    for key in graph:
        visit(key)
    return graph


@app.post("/workflows", status_code=201)
def create_workflow(body: WorkflowCreate) -> dict:
    graph = _validate_dag(body.tasks)
    workspaces = {}
    for item in body.tasks:
        workspace = Path(item.workspace_path).expanduser().resolve()
        if not workspace.is_dir():
            raise HTTPException(status_code=422, detail=f"workspace_path for task {item.key!r} must be an existing directory")
        workspaces[item.key] = str(workspace)

    with SessionLocal() as db:
        workflow = Workflow(title=body.title.strip(), status="queued")
        db.add(workflow)
        db.flush()
        created = {}
        for item in body.tasks:
            task = Task(
                workflow_id=workflow.id, title=item.title.strip(), prompt=item.prompt,
                workspace_path=workspaces[item.key], permission_mode=item.permission_mode,
                priority=item.priority, max_attempts=item.max_attempts,
                acceptance_criteria=item.acceptance_criteria,
                verification_commands=item.verification_commands,
                auto_repair=item.auto_repair,
                max_repair_attempts=item.max_repair_attempts if item.auto_repair else 0,
                verification_status="pending" if item.verification_commands else "not_configured",
            )
            db.add(task)
            created[item.key] = task
        db.flush()
        for child, parents in graph.items():
            for parent in parents:
                db.add(WorkflowTaskDependency(
                    workflow_id=workflow.id,
                    upstream_task_id=created[parent].id,
                    downstream_task_id=created[child].id,
                ))
        for task in created.values():
            db.add(TaskEvent(task_id=task.id, event_type="task.queued", payload={
                "status": "queued", "workflow_id": workflow.id,
            }))
        db.commit()
        db.refresh(workflow)
        return {
            **_workflow_payload(db, workflow),
            "tasks": [{"key": key, "task": {
                "id": task.id, "title": task.title, "status": task.status,
                "workflow_id": task.workflow_id,
            }} for key, task in created.items()],
            "dependencies": [
                {"upstream_task_id": created[parent].id, "downstream_task_id": created[child].id}
                for child, parents in graph.items() for parent in parents
            ],
        }


@app.get("/workflows")
def list_workflows(limit: int = Query(default=50, ge=1, le=200)) -> list[dict]:
    with SessionLocal() as db:
        workflows = list(db.scalars(select(Workflow).order_by(Workflow.created_at.desc()).limit(limit)))
        return [_workflow_payload(db, workflow) for workflow in workflows]


@app.get("/workflows/{workflow_id}")
def get_workflow(workflow_id: str) -> dict:
    with SessionLocal() as db:
        workflow = db.get(Workflow, workflow_id)
        if workflow is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        tasks = list(db.scalars(select(Task).where(Task.workflow_id == workflow_id).order_by(Task.created_at, Task.id)))
        edges = list(db.scalars(select(WorkflowTaskDependency).where(WorkflowTaskDependency.workflow_id == workflow_id)))
        return {
            **_workflow_payload(db, workflow),
            "tasks": [TaskRead.model_validate(task).model_dump(mode="json") | {
                "workflow_id": task.workflow_id, "blocked_reason": task.blocked_reason,
            } for task in tasks],
            "dependencies": [
                {"upstream_task_id": edge.upstream_task_id, "downstream_task_id": edge.downstream_task_id}
                for edge in edges
            ],
        }


@app.post("/workflows/{workflow_id}/cancel")
def cancel_workflow(workflow_id: str) -> dict:
    with SessionLocal() as db:
        workflow = db.get(Workflow, workflow_id)
        if workflow is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        if workflow.status in {"succeeded", "failed", "cancelled"}:
            return _workflow_payload(db, workflow)
        timestamp = utcnow()
        # Publish workflow cancellation first. The worker's atomic claim checks this
        # state, so a task cannot be newly claimed after this transaction commits.
        workflow.status = "cancelled"
        workflow.finished_at = timestamp
        tasks = list(db.scalars(select(Task.id).where(
            Task.workflow_id == workflow_id, Task.status.in_(["queued", "running"])
        )))
        for task_id in tasks:
            queued_result = db.execute(
                update(Task)
                .where(Task.id == task_id, Task.status == "queued")
                .values(
                    status="cancelled", cancel_requested=1, finished_at=timestamp,
                    error_summary="Workflow cancelled before task execution",
                )
            )
            if queued_result.rowcount == 1:
                db.add(TaskEvent(task_id=task_id, event_type="task.cancelled", payload={
                    "status": "cancelled", "cancelled_before_start": True, "workflow_id": workflow_id,
                }))
                continue
            running_result = db.execute(
                update(Task)
                .where(Task.id == task_id, Task.status == "running", Task.cancel_requested == 0)
                .values(cancel_requested=1)
            )
            if running_result.rowcount == 1:
                db.add(TaskEvent(task_id=task_id, event_type="task.cancellation_requested", payload={
                    "reason": "workflow_cancelled", "workflow_id": workflow_id,
                }))
        db.commit()
        db.refresh(workflow)
        return _workflow_payload(db, workflow)
