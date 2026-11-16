from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field, field_validator


class TaskCreate(BaseModel):
    project_id: str | None = None
    title: str = Field(min_length=1, max_length=240)
    prompt: str = Field(min_length=1, max_length=100_000)
    workspace_path: str = Field(min_length=1, max_length=4096)
    permission_mode: Literal["plan", "configured"] = "plan"
    priority: int = Field(default=0, ge=0, le=100)
    max_attempts: int = Field(default=1, ge=1, le=3)
    acceptance_criteria: str | None = Field(default=None, max_length=10_000)
    verification_commands: list[str] = Field(default_factory=list, max_length=5)
    auto_repair: bool = False
    max_repair_attempts: int = Field(default=0, ge=0, le=2)

    @field_validator("verification_commands")
    @classmethod
    def validate_verification_commands(cls, commands: list[str]) -> list[str]:
        normalized = []
        for command in commands:
            command = command.strip()
            if not command:
                continue
            if len(command) > 2000 or "\x00" in command:
                raise ValueError("each verification command must be at most 2000 characters and contain no NUL")
            normalized.append(command)
        return normalized


class TaskRead(BaseModel):
    project_id: str | None = None
    id: str
    title: str
    prompt: str
    workspace_path: str
    status: str
    permission_mode: str = "plan"
    priority: int = 0
    max_attempts: int = 1
    attempt_count: int = 0
    acceptance_criteria: str | None = None
    verification_commands: list[str] = Field(default_factory=list)
    verification_status: str = "not_configured"
    auto_repair: bool = False
    max_repair_attempts: int = 0
    repair_attempt_count: int = 0
    available_at: datetime | None = None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    exit_code: int | None
    session_id: str | None
    error_summary: str | None
    cancel_requested: int
    workflow_id: str | None = None
    blocked_reason: str | None = None
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

    
class WorkflowTaskCreate(TaskCreate):
    key: str = Field(min_length=1, max_length=80)
    depends_on: list[str] = Field(default_factory=list, max_length=50)


class WorkflowCreate(BaseModel):
    project_id: str | None = None
    title: str = Field(min_length=1, max_length=240)
    tasks: list[WorkflowTaskCreate] = Field(min_length=1, max_length=100)


class WorkflowTaskRead(TaskRead):
    workflow_id: str | None = None
    blocked_reason: str | None = None


class WorkflowRead(BaseModel):
    project_id: str | None = None
    id: str
    title: str
    status: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    counts: dict[str, int]
    model_config = {"from_attributes": True}

class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    slug: str | None = Field(default=None, min_length=1, max_length=180)
    description: str | None = Field(default=None, max_length=10_000)
    workspace_path: str = Field(min_length=1, max_length=4096)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    slug: str | None = Field(default=None, min_length=1, max_length=180)
    description: str | None = Field(default=None, max_length=10_000)
    workspace_path: str | None = Field(default=None, min_length=1, max_length=4096)
    status: Literal["active", "archived"] | None = None


class ProjectRead(BaseModel):
    id: str
    name: str
    slug: str
    description: str | None
    workspace_path: str
    status: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    counts: dict[str, dict[str, int]]
    model_config = {"from_attributes": True}

class ConversationCreate(BaseModel):
    project_id: str | None = None
    title: str | None = Field(default=None, min_length=1, max_length=240)


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    status: Literal["active", "archived"] | None = None


class ConversationMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    client_message_id: str | None = Field(default=None, min_length=1, max_length=128)


class ConversationRead(BaseModel):
    id: str
    project_id: str | None
    title: str
    status: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    message_count: int
    pending_turn_count: int
    last_message: dict | None = None


class ConversationMessageRead(BaseModel):
    id: int
    conversation_id: str
    role: str
    content: str
    client_message_id: str | None
    metadata: dict = Field(validation_alias="message_metadata")
    created_at: datetime
    model_config = {"from_attributes": True}


class AgentTurnRead(BaseModel):
    id: str
    conversation_id: str
    user_message_id: int
    assistant_message_id: int | None
    status: str
    adapter: str
    cli_session_id: str | None
    started_at: datetime | None
    finished_at: datetime | None
    error_summary: str | None
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class AgentTurnEventRead(BaseModel):
    id: int
    turn_id: str
    occurred_at: datetime
    event_type: str
    payload: dict
    model_config = {"from_attributes": True}

