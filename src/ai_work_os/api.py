from pathlib import Path
from fastapi.responses import FileResponse
from fastapi import FastAPI, HTTPException, Query
from sqlalchemy import func, select
from .db import Base, SessionLocal, engine, ensure_schema_compatibility
from .models import Task, TaskEvent, utcnow
from .schemas import TaskCreate, TaskRead, TaskEventRead

Base.metadata.create_all(bind=engine)
ensure_schema_compatibility()
app = FastAPI(title="AI Work OS API", version="0.2.0")

WEB_DIR = Path(__file__).resolve().parent / "web"


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
    task = Task(title=body.title.strip(), prompt=body.prompt, workspace_path=str(workspace), permission_mode=body.permission_mode)
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

@app.post("/tasks/{task_id}/cancel", response_model=TaskRead)
def cancel_task(task_id: str) -> Task:
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        if task.status in {"queued", "running"}:
            task.cancel_requested = 1
            if task.status == "queued":
                # A queued task has no worker to observe the cancellation flag.
                task.status = "cancelled"
                task.finished_at = utcnow()
                db.add(TaskEvent(
                    task_id=task.id,
                    event_type="task.cancelled",
                    payload={"status": "cancelled", "cancelled_before_start": True},
                ))
            else:
                db.add(TaskEvent(
                    task_id=task.id,
                    event_type="task.cancel_requested",
                    payload={"status": "running"},
                ))
            db.commit()
            db.refresh(task)
        return task
