from __future__ import annotations

import os
from pathlib import Path
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DB_URL = os.getenv("AI_WORK_OS_DB", "./storage/ai-work-os.db")
if DB_URL.startswith("sqlite:///"):
    Path(DB_URL.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
elif "://" not in DB_URL:
    Path(DB_URL).parent.mkdir(parents=True, exist_ok=True)
    DB_URL = f"sqlite:///{DB_URL}"

_connect_args = {"check_same_thread": False} if DB_URL.startswith("sqlite") else {}
engine = create_engine(DB_URL, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def ensure_schema_compatibility() -> None:
    """Apply small additive migrations needed by existing local SQLite databases."""
    inspector = inspect(engine)
    if not inspector.has_table("tasks"):
        return
    columns = {column["name"] for column in inspector.get_columns("tasks")}
    workflow_columns = {column["name"] for column in inspector.get_columns("workflows")} if inspector.has_table("workflows") else set()
    with engine.begin() as connection:
        if "project_id" not in columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN project_id VARCHAR(36) REFERENCES projects(id) ON DELETE SET NULL"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_tasks_project_id ON tasks (project_id)"))
        if "project_id" not in workflow_columns:
            connection.execute(text("ALTER TABLE workflows ADD COLUMN project_id VARCHAR(36) REFERENCES projects(id) ON DELETE SET NULL"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_workflows_project_id ON workflows (project_id)"))
        if "workflow_id" not in columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN workflow_id VARCHAR(36) REFERENCES workflows(id)"))
        if "blocked_reason" not in columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN blocked_reason TEXT"))
        if "worker_id" not in columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN worker_id VARCHAR(36)"))
        if "lease_expires_at" not in columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN lease_expires_at DATETIME"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_tasks_workflow_id ON tasks (workflow_id)"))
        if "permission_mode" not in columns:
            connection.execute(text(
                "ALTER TABLE tasks ADD COLUMN permission_mode VARCHAR(32) NOT NULL DEFAULT 'plan'"
            ))
        if "priority" not in columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 0"))
        if "max_attempts" not in columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 1"))
        if "attempt_count" not in columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0"))
        if "available_at" not in columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN available_at DATETIME"))
        if "acceptance_criteria" not in columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN acceptance_criteria TEXT"))
        if "verification_commands" not in columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN verification_commands JSON NOT NULL DEFAULT '[]'"))
        if "verification_status" not in columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN verification_status VARCHAR(32) NOT NULL DEFAULT 'not_configured'"))
        if "auto_repair" not in columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN auto_repair INTEGER NOT NULL DEFAULT 0"))
        if "max_repair_attempts" not in columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN max_repair_attempts INTEGER NOT NULL DEFAULT 0"))
        if "repair_attempt_count" not in columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN repair_attempt_count INTEGER NOT NULL DEFAULT 0"))
        connection.execute(text("UPDATE tasks SET available_at = created_at WHERE available_at IS NULL"))

    if inspector.has_table("conversations"):
        conversation_columns = {
            column["name"] for column in inspector.get_columns("conversations")
        }
        with engine.begin() as connection:
            if "cli_session_id" not in conversation_columns:
                connection.execute(text(
                    "ALTER TABLE conversations ADD COLUMN cli_session_id VARCHAR(128)"
                ))
            if inspector.has_table("agent_turns"):
                connection.execute(text("""
                    UPDATE conversations
                    SET cli_session_id = (
                        SELECT agent_turns.cli_session_id
                        FROM agent_turns
                        WHERE agent_turns.conversation_id = conversations.id
                          AND agent_turns.cli_session_id IS NOT NULL
                        ORDER BY agent_turns.created_at DESC, agent_turns.id DESC
                        LIMIT 1
                    )
                    WHERE cli_session_id IS NULL
                      AND EXISTS (
                        SELECT 1 FROM agent_turns
                        WHERE agent_turns.conversation_id = conversations.id
                          AND agent_turns.cli_session_id IS NOT NULL
                      )
                """))

    if inspector.has_table("agent_turns"):
        turn_columns = {column["name"] for column in inspector.get_columns("agent_turns")}
        with engine.begin() as connection:
            if "worker_id" not in turn_columns:
                connection.execute(text("ALTER TABLE agent_turns ADD COLUMN worker_id VARCHAR(36)"))
            if "lease_expires_at" not in turn_columns:
                connection.execute(text("ALTER TABLE agent_turns ADD COLUMN lease_expires_at DATETIME"))
            if "cancel_requested" not in turn_columns:
                connection.execute(text("ALTER TABLE agent_turns ADD COLUMN cancel_requested BOOLEAN NOT NULL DEFAULT 0"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_agent_turns_worker_id ON agent_turns (worker_id)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_agent_turns_lease_expires_at ON agent_turns (lease_expires_at)"))
