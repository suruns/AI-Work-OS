"""Conversation-scoped MCP control plane for the Master Agent.

The server speaks newline-delimited JSON-RPC 2.0 over stdio and intentionally
implements only the MCP initialize/tools/list/tools/call methods needed by
Claude Code. It is launched as a child of the trusted Master Agent turn.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from sqlalchemy import func, select

from .db import Base, SessionLocal, engine, ensure_schema_compatibility
from .models import (
    Project, Task, TaskEvent, Workflow, WorkflowTaskDependency, utcnow,
)

Base.metadata.create_all(bind=engine)
ensure_schema_compatibility()

PROJECT_ID = os.getenv("AI_WORK_OS_MCP_PROJECT_ID", "").strip()
PROTOCOL_VERSION = "2024-11-05"
MAX_MESSAGE_BYTES = 1_000_000
MAX_LIMIT = 50


class ToolError(ValueError):
    pass


def _project(db) -> Project:
    if not PROJECT_ID:
        raise ToolError("This conversation is not attached to a project.")
    project = db.get(Project, PROJECT_ID)
    if project is None:
        raise ToolError("The conversation project no longer exists.")
    return project


def _task_summary(task: Task) -> dict[str, Any]:
    return {
        "id": task.id, "title": task.title, "status": task.status,
        "priority": task.priority, "workflow_id": task.workflow_id,
        "permission_mode": task.permission_mode,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "error_summary": task.error_summary,
    }


def _workflow_summary(db, workflow: Workflow) -> dict[str, Any]:
    rows = db.execute(
        select(Task.status, func.count()).where(
            Task.workflow_id == workflow.id
        ).group_by(Task.status)
    ).all()
    return {
        "id": workflow.id, "title": workflow.title, "status": workflow.status,
        "counts": {status: count for status, count in rows},
        "created_at": workflow.created_at.isoformat() if workflow.created_at else None,
    }


def _tools() -> list[dict[str, Any]]:
    return [
        {"name": "get_project_overview", "description": "Read the current conversation's project summary.",
         "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"name": "list_tasks", "description": "List tasks in the current project.",
         "inputSchema": {"type": "object", "properties": {
             "status": {"type": "string", "enum": ["queued", "running", "succeeded", "failed", "cancelled", "blocked"]},
             "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 20}},
             "additionalProperties": False}},
        {"name": "get_task", "description": "Read a task in the current project.",
         "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}},
                         "required": ["task_id"], "additionalProperties": False}},
        {"name": "list_workflows", "description": "List workflows in the current project.",
         "inputSchema": {"type": "object", "properties": {
             "status": {"type": "string", "enum": ["queued", "running", "succeeded", "failed", "cancelled"]},
             "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 20}},
             "additionalProperties": False}},
        {"name": "get_workflow", "description": "Read a workflow and its tasks in the current project.",
         "inputSchema": {"type": "object", "properties": {"workflow_id": {"type": "string"}},
                         "required": ["workflow_id"], "additionalProperties": False}},
        {"name": "create_task", "description": "Queue a plan-mode task in the current project. It will not execute until claimed by a Worker.",
         "inputSchema": {"type": "object", "properties": {
             "title": {"type": "string", "minLength": 1, "maxLength": 240},
             "prompt": {"type": "string", "minLength": 1, "maxLength": 100000},
             "acceptance_criteria": {"type": "string", "maxLength": 10000}},
             "required": ["title", "prompt"], "additionalProperties": False}},
        {"name": "create_workflow", "description": "Queue a plan-mode workflow in the current project. Dependencies use task keys.",
         "inputSchema": {"type": "object", "properties": {
             "title": {"type": "string", "minLength": 1, "maxLength": 240},
             "tasks": {"type": "array", "minItems": 1, "maxItems": 30, "items": {
                 "type": "object", "properties": {
                     "key": {"type": "string", "minLength": 1, "maxLength": 80},
                     "title": {"type": "string", "minLength": 1, "maxLength": 240},
                     "prompt": {"type": "string", "minLength": 1, "maxLength": 100000},
                     "depends_on": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
                     "acceptance_criteria": {"type": "string", "maxLength": 10000}},
                 "required": ["key", "title", "prompt"], "additionalProperties": False}},
             "required": ["title", "tasks"], "additionalProperties": False}},
    ]


def _create_task(db, project: Project, args: dict) -> dict:
    if project.status != "active":
        raise ToolError("Archived projects cannot receive new work.")
    title = args.get("title")
    prompt = args.get("prompt")
    criteria = args.get("acceptance_criteria")
    if not isinstance(title, str) or not title.strip() or len(title) > 240:
        raise ToolError("title must be a non-empty string of at most 240 characters")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 100_000:
        raise ToolError("prompt must be a non-empty string of at most 100000 characters")
    if criteria is not None and (not isinstance(criteria, str) or len(criteria) > 10_000):
        raise ToolError("acceptance_criteria must be a string of at most 10000 characters")
    workspace = project.workspace_path
    task = Task(
        project_id=project.id, title=title.strip(), prompt=prompt,
        workspace_path=workspace, permission_mode="plan", priority=0,
        max_attempts=1, acceptance_criteria=criteria,
        verification_commands=[], auto_repair=False, max_repair_attempts=0,
        verification_status="not_configured",
    )
    db.add(task)
    db.flush()
    db.add(TaskEvent(task_id=task.id, event_type="task.queued", payload={
        "status": "queued", "project_id": project.id,
        "created_via": "master_agent_mcp",
    }))
    db.commit()
    db.refresh(task)
    return _task_summary(task)


def _create_workflow(db, project: Project, args: dict) -> dict:
    if project.status != "active":
        raise ToolError("Archived projects cannot receive new work.")
    title, specs = args.get("title"), args.get("tasks")
    if not isinstance(title, str) or not title.strip() or len(title) > 240:
        raise ToolError("title must be a non-empty string of at most 240 characters")
    if not isinstance(specs, list) or not 1 <= len(specs) <= 30:
        raise ToolError("tasks must contain between 1 and 30 entries")
    keys: dict[str, dict] = {}
    for spec in specs:
        if not isinstance(spec, dict):
            raise ToolError("each task must be an object")
        key = spec.get("key")
        if not isinstance(key, str) or not key.strip() or len(key) > 80 or key in keys:
            raise ToolError("task keys must be unique non-empty strings of at most 80 characters")
        keys[key] = spec
    graph: dict[str, list[str]] = {}
    for key, spec in keys.items():
        parents = spec.get("depends_on", [])
        if not isinstance(parents, list) or len(parents) > 30 or any(
            not isinstance(parent, str) or parent not in keys or parent == key for parent in parents
        ):
            raise ToolError(f"task {key!r} has invalid dependencies")
        if len(set(parents)) != len(parents):
            raise ToolError(f"task {key!r} has duplicate dependencies")
        graph[key] = parents
        for field, maximum in (("title", 240), ("prompt", 100000), ("acceptance_criteria", 10000)):
            value = spec.get(field)
            if field in {"title", "prompt"} and (not isinstance(value, str) or not value.strip()):
                raise ToolError(f"task {key!r} requires {field}")
            if value is not None and (not isinstance(value, str) or len(value) > maximum):
                raise ToolError(f"task {key!r} has invalid {field}")
    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(key: str) -> None:
        if key in visiting:
            raise ToolError("workflow dependencies must be acyclic")
        if key in visited:
            return
        visiting.add(key)
        for parent in graph[key]:
            visit(parent)
        visiting.remove(key)
        visited.add(key)
    for key in graph:
        visit(key)

    workflow = Workflow(project_id=project.id, title=title.strip(), status="queued")
    db.add(workflow)
    db.flush()
    created: dict[str, Task] = {}
    for key, spec in keys.items():
        task = Task(
            project_id=project.id, workflow_id=workflow.id,
            title=spec["title"].strip(), prompt=spec["prompt"],
            workspace_path=project.workspace_path, permission_mode="plan",
            priority=0, max_attempts=1,
            acceptance_criteria=spec.get("acceptance_criteria"),
            verification_commands=[], auto_repair=False, max_repair_attempts=0,
            verification_status="not_configured",
        )
        db.add(task)
        created[key] = task
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
            "project_id": project.id, "created_via": "master_agent_mcp",
        }))
    db.commit()
    db.refresh(workflow)
    return {
        **_workflow_summary(db, workflow),
        "tasks": [{"key": key, **_task_summary(task)} for key, task in created.items()],
        "dependencies": [
            {"upstream_task_id": created[parent].id, "downstream_task_id": created[child].id}
            for child, parents in graph.items() for parent in parents
        ],
    }


def call_tool(name: str, args: dict) -> Any:
    if not isinstance(args, dict):
        raise ToolError("tool arguments must be an object")
    with SessionLocal() as db:
        project = _project(db)
        if name == "get_project_overview":
            counts = {}
            for model, key in ((Task, "tasks"), (Workflow, "workflows")):
                rows = db.execute(select(model.status, func.count()).where(
                    model.project_id == project.id
                ).group_by(model.status)).all()
                counts[key] = {status: count for status, count in rows}
            return {"id": project.id, "name": project.name, "slug": project.slug,
                    "description": project.description, "status": project.status, "counts": counts}
        if name == "list_tasks":
            limit = args.get("limit", 20)
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
                raise ToolError(f"limit must be between 1 and {MAX_LIMIT}")
            query = select(Task).where(Task.project_id == project.id).order_by(Task.created_at.desc(), Task.id).limit(limit)
            if args.get("status") is not None:
                query = query.where(Task.status == args["status"])
            return [_task_summary(task) for task in db.scalars(query)]
        if name == "get_task":
            task = db.get(Task, str(args.get("task_id", "")))
            if task is None or task.project_id != project.id:
                raise ToolError("task not found in the current project")
            return {**_task_summary(task), "prompt": task.prompt,
                    "acceptance_criteria": task.acceptance_criteria}
        if name == "list_workflows":
            limit = args.get("limit", 20)
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
                raise ToolError(f"limit must be between 1 and {MAX_LIMIT}")
            query = select(Workflow).where(Workflow.project_id == project.id).order_by(Workflow.created_at.desc(), Workflow.id).limit(limit)
            if args.get("status") is not None:
                query = query.where(Workflow.status == args["status"])
            return [_workflow_summary(db, item) for item in db.scalars(query)]
        if name == "get_workflow":
            workflow = db.get(Workflow, str(args.get("workflow_id", "")))
            if workflow is None or workflow.project_id != project.id:
                raise ToolError("workflow not found in the current project")
            tasks = list(db.scalars(select(Task).where(
                Task.workflow_id == workflow.id, Task.project_id == project.id
            ).order_by(Task.created_at, Task.id)))
            edges = list(db.scalars(select(WorkflowTaskDependency).where(
                WorkflowTaskDependency.workflow_id == workflow.id
            )))
            return {**_workflow_summary(db, workflow), "tasks": [_task_summary(task) for task in tasks],
                    "dependencies": [{"upstream_task_id": edge.upstream_task_id,
                                      "downstream_task_id": edge.downstream_task_id} for edge in edges]}
        if name == "create_task":
            return _create_task(db, project, args)
        if name == "create_workflow":
            return _create_workflow(db, project, args)
    raise ToolError(f"unknown tool: {name}")


def _reply(message: dict) -> dict | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "ai-work-os-master-control", "version": "0.1.0"},
        }}
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": _tools()}}
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            value = call_tool(name, args)
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, default=str)}],
                "isError": False,
            }}
        except (ToolError, ValueError, TypeError) as exc:
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": str(exc)}], "isError": True,
            }}
    if request_id is not None:
        return {"jsonrpc": "2.0", "id": request_id, "error": {
            "code": -32601, "message": f"Method not found: {method}",
        }}
    return None


def main() -> None:
    for raw in sys.stdin.buffer:
        if len(raw) > MAX_MESSAGE_BYTES:
            continue
        try:
            message = json.loads(raw)
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                continue
            response = _reply(message)
        except Exception as exc:
            request_id = None
            try:
                request_id = message.get("id")
            except UnboundLocalError:
                pass
            response = {"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32603, "message": type(exc).__name__}}
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
