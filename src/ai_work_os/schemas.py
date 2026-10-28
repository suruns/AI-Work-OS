from datetime import datetime
from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    prompt: str = Field(min_length=1, max_length=100_000)
    workspace_path: str = Field(min_length=1, max_length=4096)


class TaskRead(BaseModel):
    id: str
    title: str
    prompt: str
    workspace_path: str
    status: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    exit_code: int | None
    session_id: str | None
    error_summary: str | None
    cancel_requested: int
    queue_wait_ms: int | None = None
    execution_ms: int | None = None
    model_config = {"from_attributes": True}


class TaskEventRead(BaseModel):
    id: int
    task_id: str
    occurred_at: datetime
    event_type: str
    payload: dict
    model_config = {"from_attributes": True}
