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
    Project, Task, TaskEvent, Workflow, utcnow,
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
            if existing.project_id != project_id or existing.goal.strip() != goal.strip():
                raise MasterRunError("当前对话已有不同的活动目标；请先继续或结束该目标。")
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
    if not isinstance(increment_step, bool):
        raise MasterRunError("increment_step 必须是布尔值。")
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


def extend_run_budget(
    run_id: str, conversation_id: str, project_id: str, *,
    max_steps: int | None = None, deadline_hours: int | None = None, session=None,
) -> dict:
    """Explicitly extend a blocked goal's bounded execution budget."""
    if max_steps is not None and (
        not isinstance(max_steps, int) or isinstance(max_steps, bool)
        or not 1 <= max_steps <= MAX_MAX_STEPS
    ):
        raise MasterRunError(f"max_steps 必须介于 1 和 {MAX_MAX_STEPS} 之间。")
    if deadline_hours is not None and (
        not isinstance(deadline_hours, int) or isinstance(deadline_hours, bool)
        or not 1 <= deadline_hours <= 24
    ):
        raise MasterRunError("deadline_hours 必须介于 1 和 24 之间。")
    if max_steps is None and deadline_hours is None:
        raise MasterRunError("必须提供 max_steps 或 deadline_hours 以扩展预算。")
    with (nullcontext(session) if session is not None else SessionLocal()) as db:
        _assert_scope(db, conversation_id, project_id)
        run = db.get(MasterRun, run_id)
        if run is None or run.conversation_id != conversation_id or run.project_id != project_id:
            raise MasterRunError("当前对话中不存在该目标。")
        if run.status in TERMINAL_STATUSES:
            raise MasterRunError("已结束的目标不能扩展预算。")
        if max_steps is not None:
            if max_steps <= run.max_steps or max_steps <= run.step_count:
                raise MasterRunError("新的 max_steps 必须大于当前上限和已用步骤数。")
            run.max_steps = max_steps
        if deadline_hours is not None:
            run.deadline_at = utcnow() + timedelta(hours=deadline_hours)
        checkpoint = dict(run.checkpoint or {})
        stop_reason = checkpoint.get("stop_reason")
        if stop_reason == "step_budget_exceeded" and max_steps is None:
            raise MasterRunError("步骤预算耗尽；必须明确提高 max_steps 才能恢复。")
        if stop_reason == "deadline_exceeded" and deadline_hours is None:
            raise MasterRunError("目标时限已过；必须明确重设 deadline_hours 才能恢复。")
        if stop_reason in {"deadline_exceeded", "step_budget_exceeded"}:
            checkpoint.pop("stop_reason", None)
            if run.status == "blocked":
                run.status = "running"
        checkpoint["budget_extended_at"] = utcnow().isoformat()
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


def cancel_run(
    run_id: str, conversation_id: str, project_id: str, *,
    reason: str | None = None, session=None,
) -> dict:
    """Cancel a goal and request cancellation of every linked queued/running task."""
    timestamp = utcnow()
    with (nullcontext(session) if session is not None else SessionLocal()) as db:
        _assert_scope(db, conversation_id, project_id)
        run = db.get(MasterRun, run_id)
        if run is None or run.conversation_id != conversation_id or run.project_id != project_id:
            raise MasterRunError("当前对话中不存在该目标。")
        if run.status in {"succeeded", "failed"}:
            raise MasterRunError("已完成或失败的目标不能取消。")
        if run.status == "cancelled":
            return _summary(run)

        workflow_ids = list(db.scalars(select(MasterRunWorkflow.workflow_id).where(
            MasterRunWorkflow.master_run_id == run_id,
        )))
        for workflow_id in workflow_ids:
            workflow = db.get(Workflow, workflow_id)
            if workflow is not None and workflow.status not in TERMINAL_STATUSES:
                workflow.status = "cancelled"
                workflow.finished_at = timestamp

        task_ids = list(db.scalars(select(MasterRunTask.task_id).where(
            MasterRunTask.master_run_id == run_id,
        )))
        cancelled_tasks = 0
        cancellation_requested = 0
        for task_id in task_ids:
            task = db.get(Task, task_id)
            if task is None or task.project_id != project_id:
                continue
            if task.status == "queued":
                task.status = "cancelled"
                task.cancel_requested = True
                task.finished_at = timestamp
                task.error_summary = "MasterRun cancelled before task execution"
                approval = db.scalar(select(ExecutionApproval).where(
                    ExecutionApproval.task_id == task.id,
                ))
                if approval is not None and approval.status in {"pending", "approved"}:
                    approval.status = "revoked"
                    approval.decided_at = timestamp
                    approval.decision_source = "task_state"
                    approval.decision_note = "所属目标在任务开始执行前已取消。"
                    db.add(TaskEvent(task_id=task.id, event_type="execution.approval_revoked", payload={
                        "approval_id": approval.id, "reason": "master_run_cancelled_before_start",
                    }))
                db.add(TaskEvent(task_id=task.id, event_type="task.cancelled", payload={
                    "status": "cancelled", "action": "cancel",
                    "reason": "master_run_cancelled", "cancelled_before_start": True,
                    "control_surface": "master_run",
                }))
                cancelled_tasks += 1
            elif task.status == "running" and not task.cancel_requested:
                task.cancel_requested = True
                db.add(TaskEvent(task_id=task.id, event_type="task.cancel_requested", payload={
                    "status": "requested", "action": "cancel",
                    "reason": "master_run_cancelled", "control_surface": "master_run",
                }))
                cancellation_requested += 1

        run.status = "cancelled"
        run.finished_at = timestamp
        run.updated_at = timestamp
        run.active_key = None
        run.checkpoint = {
            **(run.checkpoint or {}),
            "cancelled_at": timestamp.isoformat(),
            "cancel_reason": (reason or "user_requested")[:2000],
            "cancelled_task_count": cancelled_tasks,
            "cancellation_requested_task_count": cancellation_requested,
        }
        if session is None:
            db.commit()
            db.refresh(run)
        else:
            db.flush()
        return {**_summary(run), "cancelled_tasks": cancelled_tasks,
                "cancellation_requested_tasks": cancellation_requested}


def link_task(run_id: str, conversation_id: str, project_id: str, task_id: str, *, required: bool = True) -> None:
    with SessionLocal() as db:
        _assert_scope(db, conversation_id, project_id)
        run = db.get(MasterRun, run_id)
        task = db.get(Task, task_id)
        if run is None or run.conversation_id != conversation_id or run.project_id != project_id:
            raise MasterRunError("当前对话中不存在该目标。")
        if run.status in TERMINAL_STATUSES:
            raise MasterRunError("已结束的目标不能再关联任务。")
        if not isinstance(required, bool):
            raise MasterRunError("required 必须是布尔值。")
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
        if run.status in TERMINAL_STATUSES:
            raise MasterRunError("已结束的目标不能再关联工作流。")
        if workflow is None or workflow.project_id != project_id:
            raise MasterRunError("工作流不属于当前项目。")
        existing = db.scalar(select(MasterRunWorkflow).where(
            MasterRunWorkflow.master_run_id == run_id, MasterRunWorkflow.workflow_id == workflow_id
        ))
        if existing is None:
            db.add(MasterRunWorkflow(master_run_id=run_id, workflow_id=workflow_id))
            db.commit()


def _effective_approval_status(status: str | None, expires_at) -> str | None:
    if status != "pending" or expires_at is None:
        return status
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return "expired" if expires_at <= utcnow() else status


def list_run_tasks(run_id: str, conversation_id: str, project_id: str) -> list[dict]:
    with SessionLocal() as db:
        _assert_scope(db, conversation_id, project_id)
        run = db.get(MasterRun, run_id)
        if run is None or run.conversation_id != conversation_id or run.project_id != project_id:
            raise MasterRunError("当前对话中不存在该目标。")
        rows = db.execute(
            select(MasterRunTask, Task, ExecutionApproval.status, ExecutionApproval.expires_at)
            .join(Task, Task.id == MasterRunTask.task_id)
            .outerjoin(ExecutionApproval, ExecutionApproval.task_id == Task.id)
            .where(MasterRunTask.master_run_id == run_id, Task.project_id == project_id)
            .order_by(MasterRunTask.created_at, MasterRunTask.id)
        ).all()
        task_ids = [task.id for _, task, _, _ in rows]
        evidence_by_task: dict[str, dict[str, Any]] = {}
        if task_ids:
            event_rows = db.execute(
                select(TaskEvent.task_id, TaskEvent.event_type, TaskEvent.payload)
                .where(
                    TaskEvent.task_id.in_(task_ids),
                    TaskEvent.event_type.in_({
                        "workspace.changes_reported", "verification.finished", "executor.finished",
                    }),
                )
                .order_by(TaskEvent.id)
            ).all()
            for task_id, event_type, payload in event_rows:
                payload = payload if isinstance(payload, dict) else {}
                evidence = evidence_by_task.setdefault(task_id, {})
                if event_type == "workspace.changes_reported":
                    evidence["workspace_changes"] = {
                        key: payload.get(key) for key in (
                            "available", "changed_file_count", "report_path",
                            "report_written", "preexisting_file_count", "report_write_error",
                        ) if key in payload
                    }
                elif event_type == "verification.finished":
                    commands = payload.get("commands", [])
                    commands = commands if isinstance(commands, list) else []
                    evidence["verification"] = {
                        "status": payload.get("status"),
                        "command_count": len(commands),
                        "passed_count": sum(
                            item.get("exit_code") == 0 and not item.get("timed_out")
                            for item in commands if isinstance(item, dict)
                        ),
                        "failed_count": sum(
                            item.get("exit_code") != 0 or item.get("timed_out")
                            for item in commands if isinstance(item, dict)
                        ),
                    }
                elif event_type == "executor.finished":
                    evidence["execution"] = {
                        key: payload.get(key) for key in (
                            "status", "exit_code", "terminal_reason",
                            "output_complete", "error_summary", "attempt", "max_attempts",
                        ) if key in payload
                    }
        return [{
            "task_id": task.id, "title": task.title, "status": task.status,
            "required": bool(link.required), "verification_status": task.verification_status,
            "approval_status": _effective_approval_status(approval_status, approval_expires_at),
            "acceptance_criteria": task.acceptance_criteria,
            "verification_commands": list(task.verification_commands or []),
            "error_summary": task.error_summary, "workflow_id": task.workflow_id,
            "evidence": evidence_by_task.get(task.id, {}),
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        } for link, task, approval_status, approval_expires_at in rows]


def reported_run_status(run: MasterRun, task_rows: list[dict]) -> str:
    """Do not report goal success until the Master Agent commits the terminal transition."""
    if run.status in TERMINAL_STATUSES:
        return run.status
    derived = derive_run_status(task_rows)
    return "verifying" if derived == "succeeded" else derived


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
