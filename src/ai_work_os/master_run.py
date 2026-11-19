"""Durable goal-level orchestration services for the Master Agent."""
from __future__ import annotations

from contextlib import nullcontext
from datetime import timedelta, timezone
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .db import SessionLocal
from .models import (
    Conversation, ExecutionApproval, MasterRun, MasterRunTask, MasterRunWorkflow,
    Project, Task, Workflow, utcnow,
)

ACTIVE_STATUSES = {
    "planning", "ready", "running", "waiting_approval",
    "waiting_input", "blocked", "verifying",
}
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
ALL_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES
MAX_GOAL_CHARS = 10_000
DEFAULT_MAX_STEPS = 20
MAX_MAX_STEPS = 100
DEFAULT_DEADLINE_HOURS = 4

_ALLOWED_TRANSITIONS = {
    "planning": {"planning", "ready", "running", "waiting_input", "blocked", "cancelled"},
    "ready": {"ready", "running", "waiting_approval", "waiting_input", "blocked", "cancelled"},
    "running": {"running", "waiting_approval", "waiting_input", "blocked", "verifying", "succeeded", "failed", "cancelled"},
    "waiting_approval": {"waiting_approval", "running", "blocked", "waiting_input", "cancelled"},
    "waiting_input": {"waiting_input", "planning", "ready", "running", "cancelled"},
    "blocked": {"blocked", "planning", "ready", "running", "failed", "cancelled"},
    "verifying": {"verifying", "running", "succeeded", "blocked", "failed", "cancelled"},
    "succeeded": {"succeeded"},
    "failed": {"failed"},
    "cancelled": {"cancelled"},
}


class MasterRunError(ValueError):
    pass


def _assert_scope(db, conversation_id: str, project_id: str) -> Conversation:
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise MasterRunError("当前对话不存在。")
    if conversation.project_id != project_id:
        raise MasterRunError("目标项目与当前对话绑定项目不一致。")
    if db.get(Project, project_id) is None:
        raise MasterRunError("目标项目不存在。")
    return conversation


def _summary(run: MasterRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "conversation_id": run.conversation_id,
        "project_id": run.project_id,
        "goal": run.goal,
        "status": run.status,
        "plan": run.plan or {},
        "checkpoint": run.checkpoint or {},
        "step_count": run.step_count,
        "max_steps": run.max_steps,
        "deadline_at": run.deadline_at.isoformat() if run.deadline_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


def create_or_resume_run(
    conversation_id: str, project_id: str, goal: str, *,
    max_steps: int = DEFAULT_MAX_STEPS,
    deadline_hours: int = DEFAULT_DEADLINE_HOURS,
) -> dict:
    if not isinstance(goal, str) or not goal.strip() or len(goal) > MAX_GOAL_CHARS:
        raise MasterRunError("goal 必须是非空字符串，且不超过 10000 个字符。")
    if not isinstance(max_steps, int) or isinstance(max_steps, bool) or not 1 <= max_steps <= MAX_MAX_STEPS:
        raise MasterRunError(f"max_steps 必须介于 1 和 {MAX_MAX_STEPS} 之间。")
    if not isinstance(deadline_hours, int) or isinstance(deadline_hours, bool) or not 1 <= deadline_hours <= 24:
        raise MasterRunError("deadline_hours 必须介于 1 和 24 之间。")
    with SessionLocal() as db:
        _assert_scope(db, conversation_id, project_id)
        existing = db.scalar(
            select(MasterRun).where(
                MasterRun.conversation_id == conversation_id,
                MasterRun.active_key == conversation_id,
            )
        )
        if existing is not None:
            if existing.project_id != project_id:
                raise MasterRunError("当前活动目标的项目范围不匹配。")
            if existing.goal.strip() != goal.strip():
                raise MasterRunError("当前对话已有未完成目标。请先继续或结束该目标，再创建新目标。")
            return _summary(existing)
        run = MasterRun(
            conversation_id=conversation_id,
            project_id=project_id,
            goal=goal.strip(),
            status="planning",
            plan={},
            checkpoint={},
            active_key=conversation_id,
            step_count=0,
            max_steps=max_steps,
            deadline_at=utcnow() + timedelta(hours=deadline_hours),
        )
        db.add(run)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.scalar(select(MasterRun).where(
                MasterRun.conversation_id == conversation_id,
                MasterRun.active_key == conversation_id,
            ))
            if existing is None:
                raise
            return _summary(existing)
        db.refresh(run)
        return _summary(run)


def get_run(run_id: str, conversation_id: str, project_id: str) -> dict:
    with SessionLocal() as db:
        _assert_scope(db, conversation_id, project_id)
        run = db.get(MasterRun, run_id)
        if run is None or run.conversation_id != conversation_id or run.project_id != project_id:
            raise MasterRunError("当前对话中不存在该目标。")
        return _summary(run)


def update_plan_checkpoint(
    run_id: str, conversation_id: str, project_id: str, *,
    plan: dict | None = None, checkpoint: dict | None = None,
    increment_step: bool = False, session=None,
) -> dict:
    if plan is not None:
        if not isinstance(plan, dict):
            raise MasterRunError("plan 必须是 JSON 对象。")
        try:
            plan_size = len(json.dumps(plan, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise MasterRunError("plan 必须包含可序列化的 JSON 数据。") from exc
        if plan_size > 100_000:
            raise MasterRunError("plan 大小不能超过 100 KB。")
    if checkpoint is not None:
        if not isinstance(checkpoint, dict):
            raise MasterRunError("checkpoint 必须是 JSON 对象。")
        try:
            checkpoint_size = len(json.dumps(checkpoint, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise MasterRunError("checkpoint 必须包含可序列化的 JSON 数据。") from exc
        if checkpoint_size > 32_000:
            raise MasterRunError("checkpoint 大小不能超过 32 KB。")
    with (nullcontext(session) if session is not None else SessionLocal()) as db:
        _assert_scope(db, conversation_id, project_id)
        run = db.get(MasterRun, run_id)
        if run is None or run.conversation_id != conversation_id or run.project_id != project_id:
            raise MasterRunError("当前对话中不存在该目标。")
        if run.status in TERMINAL_STATUSES:
            raise MasterRunError("已结束的目标不能再修改计划或检查点。")
        deadline_at = run.deadline_at
        if deadline_at is not None and deadline_at.tzinfo is None:
            deadline_at = deadline_at.replace(tzinfo=timezone.utc)
        if deadline_at and deadline_at < utcnow():
            run.status = "blocked"
            run.checkpoint = {**(run.checkpoint or {}), "stop_reason": "deadline_exceeded"}
            run.finished_at = None
            db.commit()
            raise MasterRunError("目标已超过时限，已停止推进。")
        if increment_step:
            if run.step_count >= run.max_steps:
                run.status = "blocked"
                run.checkpoint = {**(run.checkpoint or {}), "stop_reason": "step_budget_exceeded"}
                db.commit()
                raise MasterRunError("目标已达到推进步骤上限，已停止推进。")
            run.step_count += 1
        if plan is not None:
            run.plan = plan
        if checkpoint is not None:
            run.checkpoint = checkpoint
        run.updated_at = utcnow()
        if session is None:
            db.commit()
            db.refresh(run)
        else:
            db.flush()
        return _summary(run)


def transition_run(
    run_id: str, conversation_id: str, project_id: str,
    status: str, *, reason: str | None = None, session=None,
) -> dict:
    if status not in ALL_STATUSES:
        raise MasterRunError("目标状态无效。")
    with (nullcontext(session) if session is not None else SessionLocal()) as db:
        _assert_scope(db, conversation_id, project_id)
        run = db.get(MasterRun, run_id)
        if run is None or run.conversation_id != conversation_id or run.project_id != project_id:
            raise MasterRunError("当前对话中不存在该目标。")
        if status == "succeeded":
            evidence_rows = db.execute(
                select(MasterRunTask, Task, ExecutionApproval.status)
                .join(Task, Task.id == MasterRunTask.task_id)
                .outerjoin(ExecutionApproval, ExecutionApproval.task_id == Task.id)
                .where(MasterRunTask.master_run_id == run_id, Task.project_id == project_id)
            ).all()
            task_evidence = [{
                "status": task.status,
                "required": bool(link.required),
                "verification_status": task.verification_status,
                "approval_status": approval_status,
            } for link, task, approval_status in evidence_rows]
            if derive_run_status(task_evidence) != "succeeded":
                raise MasterRunError("缺少全部必需任务成功及验证通过的持久化证据，不能将目标标记为完成。")
        if status not in _ALLOWED_TRANSITIONS.get(run.status, set()):
            raise MasterRunError(f"不允许目标状态从 {run.status} 转换为 {status}。")
        run.status = status
        run.updated_at = utcnow()
        if reason:
            run.checkpoint = {**(run.checkpoint or {}), "status_reason": reason[:2000]}
        if status in TERMINAL_STATUSES:
            run.finished_at = utcnow()
            run.active_key = None
        else:
            run.finished_at = None
        if session is None:
            db.commit()
            db.refresh(run)
        else:
            db.flush()
        return _summary(run)


def link_task(run_id: str, conversation_id: str, project_id: str, task_id: str, *, required: bool = True) -> None:
    with SessionLocal() as db:
        _assert_scope(db, conversation_id, project_id)
        run = db.get(MasterRun, run_id)
        task = db.get(Task, task_id)
        if run is None or run.conversation_id != conversation_id or run.project_id != project_id:
            raise MasterRunError("当前对话中不存在该目标。")
        if task is None or task.project_id != project_id:
            raise MasterRunError("任务不属于当前项目。")
        existing = db.scalar(select(MasterRunTask).where(
            MasterRunTask.master_run_id == run_id, MasterRunTask.task_id == task_id
        ))
        if existing is None:
            db.add(MasterRunTask(master_run_id=run_id, task_id=task_id, required=required))
            db.commit()


def link_workflow(run_id: str, conversation_id: str, project_id: str, workflow_id: str) -> None:
    with SessionLocal() as db:
        _assert_scope(db, conversation_id, project_id)
        run = db.get(MasterRun, run_id)
        workflow = db.get(Workflow, workflow_id)
        if run is None or run.conversation_id != conversation_id or run.project_id != project_id:
            raise MasterRunError("当前对话中不存在该目标。")
        if workflow is None or workflow.project_id != project_id:
            raise MasterRunError("工作流不属于当前项目。")
        existing = db.scalar(select(MasterRunWorkflow).where(
            MasterRunWorkflow.master_run_id == run_id, MasterRunWorkflow.workflow_id == workflow_id
        ))
        if existing is None:
            db.add(MasterRunWorkflow(master_run_id=run_id, workflow_id=workflow_id))
            db.commit()


def list_run_tasks(run_id: str, conversation_id: str, project_id: str) -> list[dict]:
    with SessionLocal() as db:
        _assert_scope(db, conversation_id, project_id)
        run = db.get(MasterRun, run_id)
        if run is None or run.conversation_id != conversation_id or run.project_id != project_id:
            raise MasterRunError("当前对话中不存在该目标。")
        rows = db.execute(
            select(MasterRunTask, Task, ExecutionApproval.status)
            .join(Task, Task.id == MasterRunTask.task_id)
            .outerjoin(ExecutionApproval, ExecutionApproval.task_id == Task.id)
            .where(MasterRunTask.master_run_id == run_id, Task.project_id == project_id)
            .order_by(MasterRunTask.created_at, MasterRunTask.id)
        ).all()
        return [{
            "task_id": task.id, "title": task.title, "status": task.status,
            "required": bool(link.required), "verification_status": task.verification_status,
            "approval_status": approval_status,
            "error_summary": task.error_summary, "workflow_id": task.workflow_id,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        } for link, task, approval_status in rows]


def derive_run_status(task_rows: list[dict]) -> str:
    """Derive a conservative goal status from persisted required task states."""
    required = [row for row in task_rows if row.get("required", True)]
    if not required:
        return "planning"
    if any(row.get("approval_status") == "pending" for row in required):
        return "waiting_approval"
    if any(row.get("approval_status") in {"rejected", "revoked", "expired", "invalidated"} for row in required):
        return "blocked"
    statuses = {row.get("status") for row in required}
    if "running" in statuses:
        return "running"
    if "queued" in statuses:
        return "ready"
    if "blocked" in statuses or "failed" in statuses:
        return "blocked"
    if "cancelled" in statuses:
        return "cancelled"
    if statuses & {"succeeded"}:
        if any(row.get("verification_status") in {"failed", "error"} for row in required):
            return "blocked"
        if any(row.get("verification_status") not in {"passed", "not_required"} for row in required):
            return "verifying"
        if all(row.get("status") == "succeeded" for row in required):
            return "succeeded"
    return "verifying"
