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
    if "permission_mode" not in columns:
        with engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE tasks ADD COLUMN permission_mode VARCHAR(32) NOT NULL DEFAULT 'plan'"
            ))
