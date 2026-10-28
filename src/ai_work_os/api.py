from pathlib import Path
from fastapi.responses import FileResponse
from fastapi import FastAPI, HTTPException, Query
from sqlalchemy import select
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
    with SessionLocal() as db:
        if not db.get(Task, task_id):
            raise HTTPException(status_code=404, detail="task not found")
        events = list(db.scalars(
            select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.id)
        ))

    if include_thinking_tokens:
        return events

    thinking = [
        event for event in events
        if event.event_type == "claude.system"
        and event.payload.get("subtype") == "thinking_tokens"
    ]
    visible = [
        event for event in events
        if not (
            event.event_type == "claude.system"
            and event.payload.get("subtype") == "thinking_tokens"
        )
    ]
    if thinking:
        latest = thinking[-1]
        estimates = [
            event.payload.get("estimated_tokens")
            for event in thinking
            if isinstance(event.payload.get("estimated_tokens"), (int, float))
        ]
        visible.append({
            "id": latest.id,
            "task_id": latest.task_id,
            "occurred_at": latest.occurred_at,
            "event_type": "claude.system.thinking_tokens_summary",
            "payload": {
                "sample_count": len(thinking),
                "first_seen": thinking[0].occurred_at.isoformat(),
                "last_seen": latest.occurred_at.isoformat(),
                "latest_estimated_tokens": estimates[-1] if estimates else None,
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
