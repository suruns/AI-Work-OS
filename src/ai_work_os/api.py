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
from .models import Task, TaskEvent, Workflow, WorkflowTaskDependency, Worker, utcnow
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
        rows = db.execute(select(Task.status, func.count()).group_by(Task.status)).all()
        counts = {status: count for status, count in rows}
        return {"total": sum(counts.values()), "by_status": counts}


def _duration_sample(start, end) -> int | None:
    if start is None or end is None:
        return None
    from .models import elapsed_ms
    return elapsed_ms(start, end)


def _token_counts(value: object) -> dict[str, int]:
    """Extract only documented Claude usage token counters from result payloads."""
    keys = {
        "input_tokens", "output_tokens",
        "cache_read_input_tokens", "cache_creation_input_tokens",
    }
    totals = {key: 0 for key in keys}
    found = set()

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key in keys and isinstance(child, (int, float)) and not isinstance(child, bool):
                    if child >= 0:
                        totals[key] += int(child)
                        found.add(key)
                elif isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return {key: totals[key] for key in sorted(found)}


@app.get("/insights")
def execution_insights(period_days: int = Query(default=30, ge=1, le=365)) -> dict:
    """Aggregate persisted task/workflow timings and Claude-reported usage; never estimate cost."""
    from datetime import timedelta
    from .models import elapsed_ms

    now = utcnow()
    since = now - timedelta(days=period_days)
    with SessionLocal() as db:
        tasks = list(db.scalars(select(Task).where(Task.created_at >= since)))
        workflows = list(db.scalars(select(Workflow).where(Workflow.created_at >= since)))
        task_ids = [task.id for task in tasks]
        events = list(db.scalars(
            select(TaskEvent).where(
                TaskEvent.task_id.in_(task_ids),
                TaskEvent.event_type == "claude.result",
            ).order_by(TaskEvent.occurred_at, TaskEvent.id)
        )) if task_ids else []

        by_status: dict[str, int] = {}
        for task in tasks:
            by_status[task.status] = by_status.get(task.status, 0) + 1

        queue_samples = [
            value for task in tasks
            if (value := _duration_sample(task.created_at, task.started_at or (
                task.finished_at if task.status == "cancelled" else None
            ))) is not None
        ]
        execution_samples = [
            value for task in tasks
            if task.started_at is not None and task.finished_at is not None
            and (value := elapsed_ms(task.started_at, task.finished_at)) is not None
        ]
        end_to_end_samples = [
            value for task in tasks
            if task.finished_at is not None
            and (value := elapsed_ms(task.created_at, task.finished_at)) is not None
        ]
        terminal = [task for task in tasks if task.status in {"succeeded", "failed", "cancelled", "blocked"}]
        successful = sum(task.status == "succeeded" for task in terminal)

        workflow_durations = [
            value for workflow in workflows
            if workflow.started_at is not None and workflow.finished_at is not None
            and (value := elapsed_ms(workflow.started_at, workflow.finished_at)) is not None
        ]
        workflow_end_to_end = [
            value for workflow in workflows
            if workflow.finished_at is not None
            and (value := elapsed_ms(workflow.created_at, workflow.finished_at)) is not None
        ]
        workflow_task_durations = [
            elapsed_ms(task.started_at, task.finished_at)
            for task in tasks if task.workflow_id and task.started_at and task.finished_at
        ]
        workflow_task_durations = [value for value in workflow_task_durations if value is not None]

        cost_total = 0.0
        cost_events = 0
        token_totals = {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }
        usage_tasks: set[str] = set()
        cost_tasks: set[str] = set()
        for event in events:
            payload = event.payload if isinstance(event.payload, dict) else {}
            cost = payload.get("total_cost_usd")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
                cost_total += float(cost)
                cost_events += 1
                cost_tasks.add(event.task_id)
            usage = payload.get("usage")
            extracted = _token_counts(usage)
            if extracted:
                usage_tasks.add(event.task_id)
                for key, value in extracted.items():
                    token_totals[key] += value

        def average(values: list[int]) -> float | None:
            return round(sum(values) / len(values), 1) if values else None

        worker_data = list_workers()
        worker_snapshot = {
            "observed_at": now,
            "registered": worker_data["total"],
            "by_status": worker_data["by_status"],
            "busy_slots": sum(item["active_task_count"] for item in worker_data["workers"]),
            "reported_capacity": sum(item["concurrency"] for item in worker_data["workers"]),
            "historical_utilization_available": False,
        }
        return {
            "period_days": period_days,
            "since": since,
            "until": now,
            "tasks": {
                "total": len(tasks), "by_status": by_status,
                "terminal": len(terminal),
                "success_rate": round(successful / len(terminal), 4) if terminal else None,
                "completed_per_day": round(len(terminal) / period_days, 2),
                "queue_wait_ms": {"average": average(queue_samples), "samples": len(queue_samples)},
                "execution_ms": {"average": average(execution_samples), "samples": len(execution_samples)},
                "end_to_end_ms": {"average": average(end_to_end_samples), "samples": len(end_to_end_samples)},
            },
            "workflows": {
                "total": len(workflows),
                "by_status": {status: sum(item.status == status for item in workflows)
                              for status in sorted({item.status for item in workflows})},
                "success_rate": (
                    round(sum(item.status == "succeeded" for item in workflows
                              if item.status in {"succeeded", "failed", "cancelled"}) /
                          sum(item.status in {"succeeded", "failed", "cancelled"} for item in workflows), 4)
                    if any(item.status in {"succeeded", "failed", "cancelled"} for item in workflows) else None
                ),
                "wall_clock_ms": {"average": average(workflow_durations), "samples": len(workflow_durations)},
                "end_to_end_ms": {"average": average(workflow_end_to_end), "samples": len(workflow_end_to_end)},
                "cumulative_task_execution_ms": sum(workflow_task_durations),
                "cumulative_task_execution_note": "Sum of member task execution time; parallel DAG tasks overlap and this is not workflow wall-clock duration.",
            },
            "usage": {
                "cost_usd": round(cost_total, 6) if cost_events else None,
                "cost_source": "Claude Code result.total_cost_usd",
                "cost_result_events": cost_events,
                "tasks_with_reported_cost": len(cost_tasks),
                "token_totals": token_totals,
                "tasks_with_reported_usage": len(usage_tasks),
                "usage_result_events": len(usage_tasks),
                "availability": "reported" if cost_events or usage_tasks else "not_collected",
                "note": "Only values emitted in persisted Claude Code result events are included. Missing values are not estimated; no model-price conversion is applied.",
            },
            "workers": worker_snapshot,
            "limitations": [
                "Metrics include tasks and workflows created within the selected period.",
                "Duration averages include only records with both required timestamps; queued tasks are not execution samples.",
                "Worker data is a current last-known snapshot, not historical utilization; heartbeat history is not retained.",
                "Token and cost coverage depends on Claude Code result events that contain usage and total_cost_usd.",
            ],
        }



@app.get("/workers")
def list_workers() -> dict:
    """Return registered workers with liveness and task-lease diagnostics."""
    now = utcnow()
    heartbeat_interval = max(5, int(os.getenv("AI_WORK_OS_WORKER_HEARTBEAT_SECONDS", "10")))
    offline_after = max(15, int(os.getenv(
        "AI_WORK_OS_WORKER_OFFLINE_AFTER_SECONDS", str(heartbeat_interval * 3)
    )))
    with SessionLocal() as db:
        records = list(db.scalars(select(Worker).order_by(Worker.last_heartbeat_at.desc())))
        items = []
        for record in records:
            last_seen = record.last_heartbeat_at
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=now.tzinfo)
            age_seconds = max(0, int((now - last_seen).total_seconds()))
            active_tasks = list(db.scalars(
                select(Task).where(Task.worker_id == record.worker_id, Task.status == "running")
                .order_by(Task.started_at, Task.id)
            ))
            task_items, expired_tasks = [], []
            for task in active_tasks:
                lease = task.lease_expires_at
                if lease is not None and lease.tzinfo is None:
                    lease = lease.replace(tzinfo=now.tzinfo)
                lease_expired = lease is not None and lease <= now
                if lease_expired:
                    expired_tasks.append(task.id)
                task_items.append({
                    "id": task.id, "title": task.title, "status": task.status,
                    "started_at": task.started_at, "lease_expires_at": task.lease_expires_at,
                    "lease_expired": lease_expired,
                })
            if expired_tasks:
                display_status = "lease_expired"
            elif record.status == "offline" or age_seconds > offline_after:
                display_status = "offline"
            elif active_tasks:
                display_status = "busy"
            else:
                display_status = "idle"
            items.append({
                "worker_id": record.worker_id, "hostname": record.hostname, "pid": record.pid,
                "started_at": record.started_at, "last_heartbeat_at": record.last_heartbeat_at,
                "heartbeat_age_seconds": age_seconds, "status": display_status,
                "reported_status": record.status, "concurrency": record.concurrency,
                "active_task_count": len(active_tasks), "active_tasks": task_items,
            })
        counts = {status: sum(1 for item in items if item["status"] == status)
                  for status in ("idle", "busy", "offline", "lease_expired")}
        return {"total": len(items), "by_status": counts, "workers": items}


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



def _diagnose_task(task: Task, events: list[TaskEvent]) -> dict:
    """Build a read-only, evidence-linked diagnosis from persisted task state."""
    event_types = [event.event_type for event in events]
    payloads = [event.payload or {} for event in events]
    terminal_reasons = [p.get("terminal_reason") for p in payloads if p.get("terminal_reason")]
    timed_out = any(
        p.get("timed_out") is True
        or p.get("reason") == "timeout"
        or any(
            result.get("timed_out") is True
            for key in ("commands", "verification")
            for result in p.get(key, [])
            if isinstance(result, dict)
        )
        for p in payloads
    ) or "timeout" in terminal_reasons or any("timeout" in name for name in event_types)
    worker_lost = any(
        name in {
            "worker.orphaned_task_recovered", "worker.lease_expired",
            "executor.lease_lost", "executor.interrupted",
        }
        for name in event_types
    )
    verification_failed = (
        task.verification_status == "failed"
        or "verification_failed" in terminal_reasons
        or any(name == "verification.finished" and p.get("status") == "failed"
               for name, p in zip(event_types, payloads))
    )
    blocked = task.status == "blocked" or bool(task.blocked_reason)
    cancelled = task.status == "cancelled" or any(
        name in {"task.cancelled", "task.cancellation_requested"} for name in event_types
    )

    if blocked:
        classification, confidence = "dependency_blocked", "high"
    elif worker_lost:
        classification, confidence = "worker_lost", "high"
    elif timed_out:
        classification, confidence = "timeout", "high"
    elif cancelled:
        classification, confidence = "cancelled", "high"
    elif verification_failed:
        classification, confidence = "verification_failed", "high"
    elif task.status == "failed":
        classification, confidence = "execution_failed", "medium"
    else:
        classification, confidence = None, "unknown"

    facts = [
        {"key": "task_status", "value": task.status, "source": "tasks.status"},
        {"key": "attempt_count", "value": task.attempt_count, "source": "tasks.attempt_count"},
        {"key": "max_attempts", "value": task.max_attempts, "source": "tasks.max_attempts"},
        {"key": "verification_status", "value": task.verification_status, "source": "tasks.verification_status"},
        {"key": "exit_code", "value": task.exit_code, "source": "tasks.exit_code"},
    ]
    if task.error_summary:
        facts.append({"key": "error_summary", "value": task.error_summary, "source": "tasks.error_summary"})
    if task.blocked_reason:
        facts.append({"key": "blocked_reason", "value": task.blocked_reason, "source": "tasks.blocked_reason"})

    hypotheses = []
    if classification == "verification_failed":
        evidence = [e.id for e in events if e.event_type == "verification.finished"
                    and (e.payload or {}).get("status") == "failed"]
        hypotheses.append({
            "message": "The agent execution reached verification, but one or more configured checks failed.",
            "evidence_event_ids": evidence,
            "recommendation": "Inspect the verification command result and decide whether the task requirements or implementation need correction.",
        })
    elif classification == "worker_lost":
        evidence = [e.id for e in events if e.event_type in {"worker.orphaned_task_recovered", "worker.lease_expired", "executor.lease_lost", "executor.interrupted"}]
        hypotheses.append({
            "message": "The worker owning this task was considered lost or its lease expired.",
            "evidence_event_ids": evidence,
            "recommendation": "Check worker process and host availability before manually deciding whether to run the task again.",
        })
    elif classification == "timeout":
        evidence = [e.id for e, p in zip(events, payloads)
                    if p.get("timed_out") is True or p.get("reason") == "timeout"
                    or any(
                        result.get("timed_out") is True
                        for key in ("commands", "verification")
                        for result in p.get(key, [])
                        if isinstance(result, dict)
                    )
                    or "timeout" in e.event_type]
        hypotheses.append({
            "message": "A recorded execution or verification step timed out.",
            "evidence_event_ids": evidence,
            "recommendation": "Review the timed-out step and its configured time limit; do not retry until repeating its side effects is safe.",
        })
    elif classification == "execution_failed":
        evidence = [e.id for e in events if e.event_type == "executor.finished"]
        hypotheses.append({
            "message": "The task ended in a failed state. The available structured records do not identify a more specific failure category.",
            "evidence_event_ids": evidence,
            "recommendation": "Inspect the task error summary and relevant attempt artifacts to identify the failing command or agent response.",
        })
    elif classification == "dependency_blocked":
        hypotheses.append({
            "message": "The task was blocked by workflow dependency state.",
            "evidence_event_ids": [e.id for e in events if e.event_type in {"workflow.task_blocked", "task.blocked"}],
            "recommendation": "Inspect the upstream workflow tasks and their terminal states.",
        })
    elif classification == "cancelled":
        hypotheses.append({
            "message": "The task was cancelled or had cancellation requested.",
            "evidence_event_ids": [e.id for e in events if e.event_type in {"task.cancelled", "task.cancellation_requested"}],
            "recommendation": "Confirm why cancellation was requested before deciding whether to create a new task.",
        })

    phase_names = {
        "task.queued": "queued", "executor.claimed": "claimed",
        "executor.started": "executing", "claude.started": "agent_running",
        "verification.started": "verifying", "verification.finished": "verification_finished",
        "repair.started": "repairing", "executor.finished": "finished",
        "task.cancelled": "cancelled", "worker.orphaned_task_recovered": "worker_recovery",
    }
    replay = [{
        "event_id": event.id,
        "occurred_at": event.occurred_at,
        "event_type": event.event_type,
        "phase": phase_names.get(event.event_type, "event"),
        "payload": event.payload or {},
        "evidence_type": "recorded_event",
    } for event in events]

    return {
        "task": {
            "id": task.id, "title": task.title, "status": task.status,
            "prompt": task.prompt, "workspace_path": task.workspace_path,
            "created_at": task.created_at, "started_at": task.started_at,
            "finished_at": task.finished_at, "exit_code": task.exit_code,
            "error_summary": task.error_summary,
            "verification_status": task.verification_status,
            "verification_commands": task.verification_commands,
            "attempt_count": task.attempt_count, "max_attempts": task.max_attempts,
        },
        "classification": classification,
        "confidence": confidence,
        "facts": facts,
        "hypotheses": hypotheses,
        "replay": replay,
        "event_count": len(events),
        "limitations": [
            "Replay contains persisted structured events only; missing events are not reconstructed.",
            "Diagnostic explanations are evidence-based hypotheses, not proof of root cause.",
            "Raw stdout/stderr contents are intentionally not included.",
        ],
    }


@app.get("/tasks/{task_id}/diagnostics")
def get_task_diagnostics(task_id: str) -> dict:
    """Return a read-only failure analysis and chronological execution replay."""
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        events = list(db.scalars(
            select(TaskEvent).where(TaskEvent.task_id == task_id)
            .order_by(TaskEvent.occurred_at, TaskEvent.id)
        ))
        return _diagnose_task(task, events)


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


@app.get("/workflows/{workflow_id}/events")
def get_workflow_events(
    workflow_id: str,
    include_thinking_tokens: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[dict]:
    """Return one chronological execution stream across every task in a workflow."""
    thinking_filter = (
        (TaskEvent.event_type == "claude.system")
        & (TaskEvent.payload["subtype"].as_string() == "thinking_tokens")
    )
    with SessionLocal() as db:
        if db.get(Workflow, workflow_id) is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        query = (
            select(TaskEvent, Task.title)
            .join(Task, Task.id == TaskEvent.task_id)
            .where(Task.workflow_id == workflow_id)
            .order_by(TaskEvent.occurred_at, TaskEvent.id)
        )
        if not include_thinking_tokens:
            query = query.where(~thinking_filter)
        rows = db.execute(query.limit(limit)).all()
        return [
            {
                "id": event.id,
                "task_id": event.task_id,
                "task_title": task_title,
                "occurred_at": event.occurred_at,
                "event_type": event.event_type,
                "payload": event.payload,
            }
            for event, task_title in rows
        ]


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
