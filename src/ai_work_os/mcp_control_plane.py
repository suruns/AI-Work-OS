"""Conversation-scoped MCP control plane for the Master Agent.

The server speaks newline-delimited JSON-RPC 2.0 over stdio and intentionally
implements only the MCP initialize/tools/list/tools/call methods needed by
Claude Code. It is launched as a child of the trusted Master Agent turn.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from datetime import timezone
from typing import Any

from sqlalchemy import func, select

BROKER_SOCKET = os.getenv("AI_WORK_OS_MCP_BROKER_SOCKET", "").strip()
if not BROKER_SOCKET:
    from .db import Base, SessionLocal, engine, ensure_schema_compatibility
    from .models import AgentTurn, MasterRun, MasterRunTask, MasterRunWorkflow, Project, Task, TaskEvent, Workflow, WorkflowTaskDependency, MCPToolInvocation, ExecutionApproval, utcnow
    from .execution_approval import DEFAULT_TTL_HOURS, task_scope_hash
    Base.metadata.create_all(bind=engine)
    ensure_schema_compatibility()
else:
    # Sandboxed MCP clients must never initialize or open the host database.
    SessionLocal = None
    Project = Task = TaskEvent = Workflow = WorkflowTaskDependency = ExecutionApproval = utcnow = None
    task_scope_hash = None
    AgentTurn = MasterRun = MasterRunTask = MasterRunWorkflow = None

PROJECT_ID = os.getenv("AI_WORK_OS_MCP_PROJECT_ID", "").strip()
TURN_ID = os.getenv("AI_WORK_OS_MCP_TURN_ID", "").strip()
PROTOCOL_VERSION = "2024-11-05"
MAX_MESSAGE_BYTES = 1_000_000
MAX_LIMIT = 50


class ToolError(ValueError):
    pass


def _project(db, project_id: str | None = None) -> Project:
    bound_project_id = project_id if project_id is not None else PROJECT_ID
    if not bound_project_id:
        raise ToolError("当前对话尚未绑定项目。")
    project = db.get(Project, bound_project_id)
    if project is None:
        raise ToolError("当前对话绑定的项目不存在。")
    return project


def _task_summary(task: Task) -> dict[str, Any]:
    return {
        "id": task.id, "title": task.title, "status": task.status,
        "priority": task.priority, "workflow_id": task.workflow_id,
        "permission_mode": task.permission_mode,
        "requires_approval": bool(task.requires_approval),
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
        {
            "name": "start_goal",
            "description": "为当前对话创建或恢复一个持久化工作目标。必须先调用此工具，再创建目标任务。",
            "inputSchema": {"type": "object", "properties": {
                "goal": {"type": "string", "minLength": 1, "maxLength": 10000},
                "max_steps": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                "deadline_hours": {"type": "integer", "minimum": 1, "maximum": 24, "default": 4},
            }, "required": ["goal"], "additionalProperties": False},
        },
        {
            "name": "cancel_goal",
            "description": "取消当前目标，并协调取消所有关联的排队/运行任务；待审批的写入申请会撤销，不会批准任何任务。",
            "inputSchema": {"type": "object", "properties": {
                "reason": {"type": "string", "maxLength": 2000},
            }, "additionalProperties": False},
        },
        {
            "name": "get_goal",
            "description": "读取当前对话的活动目标及关联任务状态。",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "update_goal",
            "description": "保存目标计划/检查点，或根据持久化任务证据更新目标状态。不能绕过审批或验证。",
            "inputSchema": {"type": "object", "properties": {
                "plan": {"type": "object"},
                "checkpoint": {"type": "object"},
                "status": {"type": "string", "enum": ["planning", "ready", "running", "waiting_approval", "waiting_input", "blocked", "verifying", "succeeded", "failed"]},
                "reason": {"type": "string", "maxLength": 2000},
                "increment_step": {"type": "boolean", "default": False},
                "max_steps": {"type": "integer", "minimum": 1, "maximum": 100},
                "deadline_hours": {"type": "integer", "minimum": 1, "maximum": 24},
            }, "additionalProperties": False},
        },
        {
            "name": "get_project_overview",
            "description": "读取当前对话绑定项目的概览信息。",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "list_tasks",
            "description": "列出当前项目的任务，可按状态筛选。",
            "inputSchema": {"type": "object", "properties": {
                "status": {"type": "string", "enum": ["queued", "running", "succeeded", "failed", "cancelled", "blocked"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 20},
            }, "additionalProperties": False},
        },
        {
            "name": "get_task",
            "description": "读取当前项目中的指定任务详情。",
            "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}},
                            "required": ["task_id"], "additionalProperties": False},
        },
        {
            "name": "list_workflows",
            "description": "列出当前项目的工作流，可按状态筛选。",
            "inputSchema": {"type": "object", "properties": {
                "status": {"type": "string", "enum": ["queued", "running", "succeeded", "failed", "cancelled"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 20},
            }, "additionalProperties": False},
        },
        {
            "name": "get_workflow",
            "description": "读取当前项目中的指定工作流及其任务。",
            "inputSchema": {"type": "object", "properties": {"workflow_id": {"type": "string"}},
                            "required": ["workflow_id"], "additionalProperties": False},
        },
        {
            "name": "create_task",
            "description": "在当前项目中创建 plan 模式任务并排队；Worker 领取后仍仅按只读权限执行。",
            "inputSchema": {"type": "object", "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 240},
                "prompt": {"type": "string", "minLength": 1, "maxLength": 100000},
                "acceptance_criteria": {"type": "string", "maxLength": 10000},
                "verification_commands": {"type": "array", "maxItems": 5, "items": {"type": "string", "minLength": 1, "maxLength": 2000}},
            }, "required": ["title", "prompt"], "additionalProperties": False},
        },
        {
            "name": "request_execution",
            "description": "在当前项目中创建 configured 模式任务并提交用户审批。此工具只申请执行，不会批准或启动任务；用户必须在本机控制台明确批准。",
            "inputSchema": {"type": "object", "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 240},
                "prompt": {"type": "string", "minLength": 1, "maxLength": 100000},
                "acceptance_criteria": {"type": "string", "maxLength": 10000},
                "verification_commands": {"type": "array", "maxItems": 5, "items": {"type": "string", "minLength": 1, "maxLength": 2000}},
            }, "required": ["title", "prompt"], "additionalProperties": False},
        },
        {
            "name": "create_workflow",
            "description": "在当前项目中创建 plan 模式工作流；任务依赖使用任务 key 表示。",
            "inputSchema": {"type": "object", "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 240},
                "tasks": {"type": "array", "minItems": 1, "maxItems": 30, "items": {
                    "type": "object", "properties": {
                        "key": {"type": "string", "minLength": 1, "maxLength": 80},
                        "title": {"type": "string", "minLength": 1, "maxLength": 240},
                        "prompt": {"type": "string", "minLength": 1, "maxLength": 100000},
                        "depends_on": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
                        "acceptance_criteria": {"type": "string", "maxLength": 10000},
                        "verification_commands": {"type": "array", "maxItems": 5, "items": {"type": "string", "minLength": 1, "maxLength": 2000}},
                    }, "required": ["key", "title", "prompt"], "additionalProperties": False,
                }},
            }, "required": ["title", "tasks"], "additionalProperties": False},
        },
    ]

def _create_task(db, project: Project, args: dict) -> dict:
    if project.status != "active":
        raise ToolError("已归档的项目不能接收新任务。")
    title = args.get("title")
    prompt = args.get("prompt")
    criteria = args.get("acceptance_criteria")
    if not isinstance(title, str) or not title.strip() or len(title) > 240:
        raise ToolError("title 必须是非空字符串，且长度不能超过 240 个字符。")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 100_000:
        raise ToolError("prompt 必须是非空字符串，且长度不能超过 100000 个字符。")
    if criteria is not None and (not isinstance(criteria, str) or len(criteria) > 10_000):
        raise ToolError("acceptance_criteria 必须是字符串，且长度不能超过 10000 个字符。")
    workspace = project.workspace_path
    commands = args.get("verification_commands", [])
    if not isinstance(commands, list) or len(commands) > 5 or any(
        not isinstance(command, str) or not command.strip() or len(command) > 2000
        for command in commands
    ):
        raise ToolError("verification_commands 最多包含 5 条命令；每条必须非空且不超过 2000 个字符。")
    task = Task(
        project_id=project.id, title=title.strip(), prompt=prompt,
        workspace_path=workspace, permission_mode="plan", priority=0,
        max_attempts=1, acceptance_criteria=criteria,
        verification_commands=commands, auto_repair=False, max_repair_attempts=0,
        verification_status="pending" if commands else "not_configured",
    )
    db.add(task)
    db.flush()
    db.add(TaskEvent(task_id=task.id, event_type="task.queued", payload={
        "status": "queued", "project_id": project.id,
        "created_via": "master_agent_mcp",
    }))
    return _task_summary(task)


def _create_workflow(db, project: Project, args: dict) -> dict:
    if project.status != "active":
        raise ToolError("已归档的项目不能接收新任务。")
    title, specs = args.get("title"), args.get("tasks")
    if not isinstance(title, str) or not title.strip() or len(title) > 240:
        raise ToolError("title 必须是非空字符串，且长度不能超过 240 个字符。")
    if not isinstance(specs, list) or not 1 <= len(specs) <= 30:
        raise ToolError("tasks 必须包含 1 到 30 个任务。")
    keys: dict[str, dict] = {}
    for spec in specs:
        if not isinstance(spec, dict):
            raise ToolError("每个 task 都必须是对象。")
        key = spec.get("key")
        if not isinstance(key, str) or not key.strip() or len(key) > 80 or key in keys:
            raise ToolError("task 的 key 必须是唯一的非空字符串，且长度不能超过 80 个字符。")
        keys[key] = spec
    graph: dict[str, list[str]] = {}
    for key, spec in keys.items():
        parents = spec.get("depends_on", [])
        if not isinstance(parents, list) or len(parents) > 30 or any(
            not isinstance(parent, str) or parent not in keys or parent == key for parent in parents
        ):
            raise ToolError(f"任务 {key!r} 的依赖配置无效。")
        if len(set(parents)) != len(parents):
            raise ToolError(f"任务 {key!r} 存在重复依赖。")
        graph[key] = parents
        for field, maximum in (("title", 240), ("prompt", 100000), ("acceptance_criteria", 10000)):
            value = spec.get(field)
            if field in {"title", "prompt"} and (not isinstance(value, str) or not value.strip()):
                raise ToolError(f"任务 {key!r} 缺少必需字段 {field}。")
            if value is not None and (not isinstance(value, str) or len(value) > maximum):
                raise ToolError(f"任务 {key!r} 的字段 {field} 无效。")
        commands = spec.get("verification_commands", [])
        if not isinstance(commands, list) or len(commands) > 5 or any(
            not isinstance(command, str) or not command.strip() or len(command) > 2000
            for command in commands
        ):
            raise ToolError(f"任务 {key!r} 的 verification_commands 最多包含 5 条有效命令。")
    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(key: str) -> None:
        if key in visiting:
            raise ToolError("工作流依赖关系不能形成环。")
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
            verification_commands=spec.get("verification_commands", []),
            auto_repair=False, max_repair_attempts=0,
            verification_status="pending" if spec.get("verification_commands") else "not_configured",
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
    return {
        **_workflow_summary(db, workflow),
        "tasks": [{"key": key, **_task_summary(task)} for key, task in created.items()],
        "dependencies": [
            {"upstream_task_id": created[parent].id, "downstream_task_id": created[child].id}
            for child, parents in graph.items() for parent in parents
        ],
    }


def call_tool(
    name: str, args: dict, request_id: str | int | float | None = None, *,
    project_id: str | None = None, turn_id: str | None = None,
) -> Any:
    if not isinstance(args, dict):
        raise ToolError("工具参数必须是 JSON 对象。")
    if SessionLocal is None:
        raise ToolError("受沙箱限制的 MCP 客户端不能直接访问数据库。")
    invocation_turn_id = turn_id or TURN_ID
    bound_project_id = project_id if project_id is not None else PROJECT_ID
    if name in {"create_task", "create_workflow", "request_execution", "start_goal", "update_goal", "cancel_goal"} and request_id is not None:
        if isinstance(request_id, bool) or not isinstance(request_id, (str, int, float)):
            raise ToolError("创建类工具调用必须提供字符串或数字类型的 JSON-RPC request id。")
        if isinstance(request_id, float) and not math.isfinite(request_id):
            raise ToolError("JSON-RPC request id 必须是有限数值。")
        request_key = ("s:" if isinstance(request_id, str) else "n:") + str(request_id)
        if not request_key or len(request_key) > 128:
            raise ToolError("JSON-RPC request id 长度必须为 1 到 128 个字符。")
        if not invocation_turn_id:
            raise ToolError("幂等创建调用必须绑定到 Master Agent 的对话轮次。")
        arguments_hash = hashlib.sha256(
            json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
    else:
        request_key = None
        arguments_hash = None

    with SessionLocal() as db:
        if request_key is not None:
            previous = db.scalar(select(MCPToolInvocation).where(
                MCPToolInvocation.turn_id == invocation_turn_id,
                MCPToolInvocation.request_id == request_key,
            ))
            if previous is not None:
                if (
                    previous.project_id != bound_project_id
                    or previous.tool_name != name
                    or previous.arguments_hash != arguments_hash
                ):
                    raise ToolError("该 JSON-RPC request id 已用于不同的工具参数或项目范围。")
                return previous.result

        if name in {"start_goal", "update_goal", "cancel_goal"}:
            if request_key is None:
                raise ToolError("目标写入工具必须提供 JSON-RPC request id。")
            from . import master_run as goal_service
            goal_service.SessionLocal = SessionLocal
            turn = db.get(AgentTurn, invocation_turn_id) if invocation_turn_id else None
            if turn is None:
                raise ToolError("目标编排工具必须绑定有效的 Master Agent 轮次。")
            project = _project(db, project_id)
            if name == "start_goal":
                value = goal_service.create_or_resume_run(
                    turn.conversation_id, project.id, args.get("goal"),
                    max_steps=args.get("max_steps", 20),
                    deadline_hours=args.get("deadline_hours", 4),
                )
            else:
                active = db.scalar(select(MasterRun).where(
                    MasterRun.conversation_id == turn.conversation_id,
                    MasterRun.active_key == turn.conversation_id,
                ))
                if active is None:
                    raise ToolError("当前对话没有活动目标。")
                if name == "cancel_goal":
                    value = goal_service.cancel_run(
                        active.id, turn.conversation_id, project.id,
                        reason=args.get("reason"), session=db,
                    )
                else:
                    if not any(key in args for key in ("plan", "checkpoint", "increment_step", "status", "max_steps", "deadline_hours")):
                        raise ToolError("update_goal 至少需要提供 plan、checkpoint、increment_step 或 status。")
                    value = goal_service.get_run(active.id, turn.conversation_id, project.id)
                    if "max_steps" in args or "deadline_hours" in args:
                        value = goal_service.extend_run_budget(
                            active.id, turn.conversation_id, project.id,
                            max_steps=args.get("max_steps"),
                            deadline_hours=args.get("deadline_hours"),
                            session=db,
                        )
                    if any(key in args for key in ("plan", "checkpoint", "increment_step")):
                        value = goal_service.update_plan_checkpoint(
                            active.id, turn.conversation_id, project.id,
                            plan=args.get("plan"), checkpoint=args.get("checkpoint"),
                            increment_step=args.get("increment_step", False), session=db,
                        )
                    if args.get("status") is not None:
                        value = goal_service.transition_run(
                            active.id, turn.conversation_id, project.id,
                            args["status"], reason=args.get("reason"), session=db,
                        )
            db.add(MCPToolInvocation(
                turn_id=invocation_turn_id, project_id=project.id,
                request_id=request_key, tool_name=name,
                arguments_hash=arguments_hash, result=value,
            ))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                previous = db.scalar(select(MCPToolInvocation).where(
                    MCPToolInvocation.turn_id == invocation_turn_id,
                    MCPToolInvocation.request_id == request_key,
                ))
                if previous is None or previous.tool_name != name or previous.arguments_hash != arguments_hash:
                    raise ToolError("目标工具调用幂等性冲突。")
                return previous.result
            return value

        project = _project(db, project_id)
        if name in {"create_task", "create_workflow", "request_execution"} and invocation_turn_id:
            turn = db.get(AgentTurn, invocation_turn_id)
            if turn is not None:
                active_run = db.scalar(select(MasterRun).where(
                    MasterRun.conversation_id == turn.conversation_id,
                    MasterRun.active_key == turn.conversation_id,
                ))
                if active_run is not None:
                    if active_run.status == "cancelling":
                        raise ToolError("当前目标正在等待关联任务停止，不能再创建新任务或工作流。")
                    checkpoint = active_run.checkpoint if isinstance(active_run.checkpoint, dict) else {}
                    deadline = active_run.deadline_at
                    if deadline is not None and deadline.tzinfo is None:
                        deadline = deadline.replace(tzinfo=timezone.utc)
                    if (
                        active_run.status == "blocked"
                        or checkpoint.get("stop_reason") in {"deadline_exceeded", "step_budget_exceeded"}
                        or (deadline is not None and deadline <= utcnow())
                        or active_run.step_count >= active_run.max_steps
                    ):
                        raise ToolError("当前目标已达到时限或推进预算上限，不能再创建新任务或工作流。")
        if name == "get_goal":
            from . import master_run as goal_service
            goal_service.SessionLocal = SessionLocal
            turn = db.get(AgentTurn, invocation_turn_id) if invocation_turn_id else None
            if turn is None:
                raise ToolError("目标查询必须绑定有效的 Master Agent 轮次。")
            active = db.scalar(select(MasterRun).where(
                MasterRun.conversation_id == turn.conversation_id,
                MasterRun.active_key == turn.conversation_id,
            ))
            if active is None:
                active = db.scalar(select(MasterRun).where(
                    MasterRun.conversation_id == turn.conversation_id,
                ).order_by(MasterRun.updated_at.desc(), MasterRun.id.desc()).limit(1))
            if active is None:
                return {"status": "no_active_goal", "derived_status": "no_active_goal", "tasks": []}
            tasks = goal_service.list_run_tasks(active.id, turn.conversation_id, project.id)
            workflows = goal_service.list_run_workflows(active.id, turn.conversation_id, project.id)
            return {
                **goal_service.get_run(active.id, turn.conversation_id, project.id),
                "derived_status": goal_service.reported_run_status(active, tasks),
                "tasks": tasks,
                "workflows": workflows,
            }

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
                raise ToolError(f"limit 必须介于 1 和 {MAX_LIMIT} 之间。")
            query = select(Task).where(Task.project_id == project.id).order_by(Task.created_at.desc(), Task.id).limit(limit)
            if args.get("status") is not None:
                query = query.where(Task.status == args["status"])
            return [_task_summary(task) for task in db.scalars(query)]
        if name == "get_task":
            task = db.get(Task, str(args.get("task_id", "")))
            if task is None or task.project_id != project.id:
                raise ToolError("当前项目中不存在该任务。")
            return {**_task_summary(task), "prompt": task.prompt,
                    "acceptance_criteria": task.acceptance_criteria}
        if name == "list_workflows":
            limit = args.get("limit", 20)
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
                raise ToolError(f"limit 必须介于 1 和 {MAX_LIMIT} 之间。")
            query = select(Workflow).where(Workflow.project_id == project.id).order_by(Workflow.created_at.desc(), Workflow.id).limit(limit)
            if args.get("status") is not None:
                query = query.where(Workflow.status == args["status"])
            return [_workflow_summary(db, item) for item in db.scalars(query)]
        if name == "get_workflow":
            workflow = db.get(Workflow, str(args.get("workflow_id", "")))
            if workflow is None or workflow.project_id != project.id:
                raise ToolError("当前项目中不存在该工作流。")
            tasks = list(db.scalars(select(Task).where(
                Task.workflow_id == workflow.id, Task.project_id == project.id
            ).order_by(Task.created_at, Task.id)))
            edges = list(db.scalars(select(WorkflowTaskDependency).where(
                WorkflowTaskDependency.workflow_id == workflow.id
            )))
            return {**_workflow_summary(db, workflow), "tasks": [_task_summary(task) for task in tasks],
                    "dependencies": [{"upstream_task_id": edge.upstream_task_id,
                                      "downstream_task_id": edge.downstream_task_id} for edge in edges]}
        if name in {"create_task", "create_workflow", "request_execution"}:
            if name == "request_execution":
                if project.status != "active":
                    raise ToolError("已归档的项目不能接收新任务。")
                title, prompt = args.get("title"), args.get("prompt")
                criteria = args.get("acceptance_criteria")
                commands = args.get("verification_commands", [])
                if not isinstance(commands, list) or len(commands) > 5 or any(
                    not isinstance(command, str) or not command.strip() or len(command) > 2000
                    for command in commands
                ):
                    raise ToolError("verification_commands 最多包含 5 条命令；每条必须非空且不超过 2000 个字符。")
                if not isinstance(title, str) or not title.strip() or len(title) > 240:
                    raise ToolError("title 必须是非空字符串，且长度不能超过 240 个字符。")
                if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 100000:
                    raise ToolError("prompt 必须是非空字符串，且长度不能超过 100000 个字符。")
                if criteria is not None and (not isinstance(criteria, str) or len(criteria) > 10000):
                    raise ToolError("acceptance_criteria 必须是字符串，且长度不能超过 10000 个字符。")
                task = Task(
                    project_id=project.id, title=title.strip(), prompt=prompt,
                    workspace_path=project.workspace_path, permission_mode="configured",
                    requires_approval=True, priority=0, max_attempts=1,
                    acceptance_criteria=criteria, verification_commands=commands,
                    auto_repair=False, max_repair_attempts=0,
                    verification_status="pending" if commands else "not_configured",
                )
                db.add(task)
                db.flush()
                approval = ExecutionApproval(
                    task_id=task.id, project_id=project.id,
                    workspace_path=project.workspace_path,
                    scope_hash=task_scope_hash(task), status="pending",
                    expires_at=utcnow() + __import__("datetime").timedelta(hours=DEFAULT_TTL_HOURS),
                )
                db.add(approval)
                db.flush()
                db.add(TaskEvent(task_id=task.id, event_type="task.queued", payload={
                    "status": "queued", "project_id": project.id, "created_via": "master_agent_mcp",
                    "requires_approval": True,
                }))
                db.add(TaskEvent(task_id=task.id, event_type="execution.approval_requested", payload={
                    "approval_id": approval.id, "project_id": project.id,
                    "permission_mode": "configured", "scope_hash": approval.scope_hash, "expires_at": approval.expires_at.isoformat(),
                }))
                value = {**_task_summary(task), "approval_id": approval.id, "approval_status": "pending"}
            else:
                value = _create_task(db, project, args) if name == "create_task" else _create_workflow(db, project, args)
            if invocation_turn_id:
                turn = db.get(AgentTurn, invocation_turn_id)
                if turn is not None:
                    active_run = db.scalar(select(MasterRun).where(
                        MasterRun.conversation_id == turn.conversation_id,
                        MasterRun.active_key == turn.conversation_id,
                    ))
                    if active_run is not None:
                        if name in {"create_task", "request_execution"}:
                            if db.scalar(select(MasterRunTask.id).where(
                                MasterRunTask.master_run_id == active_run.id,
                                MasterRunTask.task_id == value["id"],
                            )) is None:
                                db.add(MasterRunTask(
                                    master_run_id=active_run.id, task_id=value["id"], required=True,
                                ))
                        elif name == "create_workflow":
                            if db.scalar(select(MasterRunWorkflow.id).where(
                                MasterRunWorkflow.master_run_id == active_run.id,
                                MasterRunWorkflow.workflow_id == value["id"],
                            )) is None:
                                db.add(MasterRunWorkflow(
                                    master_run_id=active_run.id, workflow_id=value["id"],
                                ))
                            for created_task in value.get("tasks", []):
                                if db.scalar(select(MasterRunTask.id).where(
                                    MasterRunTask.master_run_id == active_run.id,
                                    MasterRunTask.task_id == created_task["id"],
                                )) is None:
                                    db.add(MasterRunTask(
                                        master_run_id=active_run.id,
                                        task_id=created_task["id"], required=True,
                                    ))
            if request_key is None:
                db.commit()
                return value
            db.add(MCPToolInvocation(
                turn_id=invocation_turn_id, project_id=project.id,
                request_id=request_key, tool_name=name,
                arguments_hash=arguments_hash, result=value,
            ))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                previous = db.scalar(select(MCPToolInvocation).where(
                    MCPToolInvocation.turn_id == invocation_turn_id,
                    MCPToolInvocation.request_id == request_key,
                ))
                if previous is None or previous.tool_name != name or previous.arguments_hash != arguments_hash:
                    raise ToolError("工具调用幂等性冲突：request id 已被不同参数或项目范围使用。")
                return previous.result
            return value
    raise ToolError(f"未知工具：{name}")


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
            if name in {"create_task", "create_workflow", "request_execution", "start_goal", "update_goal", "cancel_goal"} and request_id is None:
                raise ToolError("会修改数据的 MCP 工具调用必须提供 JSON-RPC request id。")
            value = (
                _broker_call(name, args, request_id)
                if BROKER_SOCKET else call_tool(name, args, request_id=request_id)
            )
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


def _broker_call(name: str, args: dict, request_id: str | int | float | None) -> Any:
    import socket
    request = json.dumps({
        "name": name, "arguments": args, "request_id": request_id,
    }, ensure_ascii=False).encode("utf-8") + b"\n"
    if len(request) > MAX_MESSAGE_BYTES:
        raise ToolError("MCP broker 请求超过消息大小限制。")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(30)
            client.connect(BROKER_SOCKET)
            client.sendall(request)
            chunks = bytearray()
            while b"\n" not in chunks:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunks) > MAX_MESSAGE_BYTES:
                    raise ToolError("MCP broker 响应超过消息大小限制。")
        response = json.loads(bytes(chunks).split(b"\n", 1)[0])
        if not isinstance(response, dict):
            raise ToolError("MCP broker 返回了无效响应。")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ToolError(f"MCP broker 不可用：{type(exc).__name__}") from exc
    if not response.get("ok"):
        raise ToolError(str(response.get("error") or "MCP broker 拒绝了请求。"))
    return response.get("value")


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
