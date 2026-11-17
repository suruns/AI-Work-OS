from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
import ipaddress
from urllib.parse import urlsplit
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .db import SessionLocal
from .models import ExecutionApproval, Project, Task, TaskEvent, utcnow

def require_local_control_plane(request: Request) -> None:
    client_host = request.client.host if request.client else ""
    if client_host == "testclient":
        return
    try:
        if not ipaddress.ip_address(client_host).is_loopback:
            raise HTTPException(status_code=403, detail="审批控制台仅允许本机访问")
    except ValueError:
        raise HTTPException(status_code=403, detail="无法确认本机访问来源")

    # A loopback client address alone is not enough: a hostile web page can
    # target localhost, and DNS rebinding can make an attacker-controlled Host
    # resolve to 127.0.0.1. Accept only explicit loopback host/origin names.
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    host_header = request.headers.get("host", "")
    try:
        request_host = urlsplit(f"//{host_header}").hostname
    except ValueError:
        request_host = None
    if request_host not in allowed_hosts:
        raise HTTPException(status_code=403, detail="审批控制台 Host 必须是 localhost 或回环地址")

    origin = request.headers.get("origin")
    if origin:
        try:
            parsed_origin = urlsplit(origin)
            origin_host = parsed_origin.hostname
        except ValueError:
            origin_host = None
            parsed_origin = None
        if (
            parsed_origin is None
            or parsed_origin.scheme not in {"http", "https"}
            or origin_host not in allowed_hosts
        ):
            raise HTTPException(status_code=403, detail="审批控制台 Origin 必须来自本机回环地址")


router = APIRouter(
    prefix="/execution-approvals", tags=["execution-approvals"],
    dependencies=[Depends(require_local_control_plane)],
)
DEFAULT_TTL_HOURS = max(1, min(168, int(os.getenv("AI_WORK_OS_APPROVAL_TTL_HOURS", "24"))))


def task_scope_hash(task: Task) -> str:
    workspace = str(Path(task.workspace_path).expanduser().resolve(strict=False))
    payload = {
        "task_id": task.id, "project_id": task.project_id, "workspace_path": workspace,
        "title": task.title, "prompt_sha256": hashlib.sha256(task.prompt.encode("utf-8")).hexdigest(),
        "permission_mode": task.permission_mode, "acceptance_criteria": task.acceptance_criteria,
        "verification_commands": list(task.verification_commands or []),
        "auto_repair": bool(task.auto_repair), "max_repair_attempts": int(task.max_repair_attempts or 0),
        "max_attempts": int(task.max_attempts or 1),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def approval_is_valid(db, task: Task) -> bool:
    if not task.requires_approval:
        return task.permission_mode == "plan"
    approval = db.scalar(select(ExecutionApproval).where(ExecutionApproval.task_id == task.id))
    if approval is None or approval.status != "approved":
        return False
    expires = approval.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    project = db.get(Project, task.project_id) if task.project_id else None
    if project is None or project.status != "active":
        return False
    task_workspace = str(Path(task.workspace_path).expanduser().resolve(strict=False))
    project_workspace = str(Path(project.workspace_path).expanduser().resolve(strict=False))
    return (
        expires > utcnow()
        and approval.scope_hash == task_scope_hash(task)
        and approval.project_id == task.project_id
        and task_workspace == project_workspace
        and approval.workspace_path == project_workspace
    )


def _payload(db, approval: ExecutionApproval) -> dict:
    task, project = db.get(Task, approval.task_id), db.get(Project, approval.project_id)
    return {
        "id": approval.id, "task_id": approval.task_id, "project_id": approval.project_id,
        "project_name": project.name if project else None, "task_title": task.title if task else None, "task_status": task.status if task else None,
        "workspace_path": approval.workspace_path, "permission_mode": task.permission_mode if task else None,
        "acceptance_criteria": task.acceptance_criteria if task else None,
        "prompt": task.prompt if task else None,
        "verification_commands": list(task.verification_commands or []) if task else [],
        "max_attempts": task.max_attempts if task else None,
        "auto_repair": bool(task.auto_repair) if task else False,
        "status": approval.status,
        "requested_at": approval.requested_at, "decided_at": approval.decided_at,
        "expires_at": approval.expires_at, "decision_source": approval.decision_source,
        "decision_note": approval.decision_note,
        "scope_valid": bool(
            task and project and project.status == "active"
            and approval.scope_hash == task_scope_hash(task)
            and approval.project_id == task.project_id
            and str(Path(task.workspace_path).expanduser().resolve(strict=False))
                == str(Path(project.workspace_path).expanduser().resolve(strict=False))
            and approval.workspace_path
                == str(Path(project.workspace_path).expanduser().resolve(strict=False))
        ),
    }


@router.post("/request/{task_id}")
def request_execution_approval(task_id: str) -> dict:
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task.status != "queued" or task.permission_mode != "configured" or not task.project_id:
            raise HTTPException(status_code=409, detail="只有绑定项目且排队中的 configured 任务可以申请写执行审批")
        existing = db.scalar(select(ExecutionApproval).where(ExecutionApproval.task_id == task.id))
        if existing:
            if existing.status in {"pending", "approved"}:
                return _payload(db, existing)
            raise HTTPException(status_code=409, detail="该任务已有终态审批记录，请创建新的执行申请")
        project = db.get(Project, task.project_id)
        if project is None or project.status != "active":
            raise HTTPException(status_code=409, detail="项目不存在或已归档")
        if str(Path(task.workspace_path).expanduser().resolve(strict=False)) != str(Path(project.workspace_path).expanduser().resolve(strict=False)):
            raise HTTPException(status_code=409, detail="任务工作区与项目工作区不一致")
        approval = ExecutionApproval(
            task_id=task.id, project_id=project.id,
            workspace_path=str(Path(task.workspace_path).expanduser().resolve(strict=False)),
            scope_hash=task_scope_hash(task), status="pending",
            expires_at=utcnow() + timedelta(hours=DEFAULT_TTL_HOURS),
        )
        task.requires_approval = True
        db.add(approval)
        db.flush()
        db.add(TaskEvent(task_id=task.id, event_type="execution.approval_requested", payload={
            "approval_id": approval.id, "project_id": project.id,
            "permission_mode": task.permission_mode, "scope_hash": approval.scope_hash, "expires_at": approval.expires_at.isoformat(),
        }))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(status_code=409, detail="该任务已存在审批申请")
        db.refresh(approval)
        return _payload(db, approval)


@router.get("")
def list_execution_approvals(status: str | None = None) -> dict:
    with SessionLocal() as db:
        if db.bind is not None and db.bind.dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        query = select(ExecutionApproval).order_by(ExecutionApproval.requested_at.desc()).with_for_update()
        if status:
            if status not in {"pending", "approved", "rejected", "revoked", "expired"}:
                raise HTTPException(status_code=400, detail="无效的审批状态")
            query = query.where(ExecutionApproval.status == status)
        records = list(db.scalars(query))
        now = utcnow()
        for record in records:
            task = db.get(Task, record.task_id)
            no_longer_eligible = (
                record.status == "pending" and (task is None or task.status != "queued")
            ) or (
                record.status == "approved" and (task is None or task.status not in {"queued", "running"})
            )
            if no_longer_eligible:
                record.status = "revoked"
                record.decided_at = now
                record.decision_source = "task_state"
                record.decision_note = "任务状态已变化，审批不再适用于该任务。"
                db.add(TaskEvent(task_id=record.task_id, event_type="execution.approval_revoked", payload={
                    "approval_id": record.id, "reason": "task_no_longer_eligible",
                }))
                continue
            expires = record.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if (
                record.status in {"pending", "approved"}
                and expires <= now
                and (task is None or task.status == "queued")
            ):
                record.status, record.decided_at = "expired", now
                db.add(TaskEvent(task_id=record.task_id, event_type="execution.approval_expired", payload={"approval_id": record.id}))
        db.commit()
        if status:
            records = [record for record in records if record.status == status]
        return {"approvals": [_payload(db, record) for record in records]}


@router.get("/{approval_id}")
def get_execution_approval(approval_id: str) -> dict:
    with SessionLocal() as db:
        approval = db.get(ExecutionApproval, approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="审批记录不存在")
        return _payload(db, approval)


def _decide(approval_id: str, decision: str, note: str | None = None) -> dict:
    with SessionLocal() as db:
        if db.bind is not None and db.bind.dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        approval_ref = db.get(ExecutionApproval, approval_id)
        if approval_ref is None:
            raise HTTPException(status_code=404, detail="审批记录不存在")
        task = db.scalar(
            select(Task).where(Task.id == approval_ref.task_id).with_for_update()
        )
        approval = db.scalar(
            select(ExecutionApproval).where(ExecutionApproval.id == approval_id).with_for_update()
        )
        if approval is None:
            raise HTTPException(status_code=404, detail="审批记录不存在")
        now = utcnow()
        if approval.status != "pending":
            raise HTTPException(status_code=409, detail="该审批已处理，不能重复决策")
        expires = approval.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= now:
            approval.status, approval.decided_at = "expired", now
            db.commit()
            raise HTTPException(status_code=409, detail="审批已过期")
        if task is None or task.status != "queued" or not task.requires_approval:
            raise HTTPException(status_code=409, detail="任务已不处于可审批状态")
        project = db.get(Project, approval.project_id)
        project_workspace = str(Path(project.workspace_path).expanduser().resolve(strict=False)) if project else ""
        task_workspace = str(Path(task.workspace_path).expanduser().resolve(strict=False))
        if (
            approval.scope_hash != task_scope_hash(task)
            or approval.project_id != task.project_id
            or project is None
            or project.status != "active"
            or project_workspace != task_workspace
            or approval.workspace_path != project_workspace
        ):
            approval.status, approval.decided_at = "revoked", now
            db.add(TaskEvent(task_id=task.id, event_type="execution.approval_scope_mismatch", payload={"approval_id": approval.id}))
            db.commit()
            raise HTTPException(status_code=409, detail="任务范围已变化，原审批已失效")
        approval.status, approval.decided_at = decision, now
        approval.decision_source = "local_control_plane"
        approval.decision_note = (note or "")[:1000] or None
        db.add(TaskEvent(task_id=task.id, event_type=f"execution.approval_{decision}", payload={
            "approval_id": approval.id, "decision_source": "local_control_plane",
            "scope_hash": approval.scope_hash, "expires_at": approval.expires_at.isoformat(),
        }))
        db.commit()
        db.refresh(approval)
        return _payload(db, approval)


@router.post("/{approval_id}/approve")
def approve_execution(approval_id: str, body: dict | None = None) -> dict:
    return _decide(approval_id, "approved", (body or {}).get("note"))


@router.post("/{approval_id}/reject")
def reject_execution(approval_id: str, body: dict | None = None) -> dict:
    return _decide(approval_id, "rejected", (body or {}).get("note"))


@router.post("/{approval_id}/revoke")
def revoke_execution(approval_id: str, body: dict | None = None) -> dict:
    with SessionLocal() as db:
        if db.bind is not None and db.bind.dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        approval_ref = db.get(ExecutionApproval, approval_id)
        if approval_ref is None:
            raise HTTPException(status_code=404, detail="审批记录不存在")
        task = db.scalar(
            select(Task).where(Task.id == approval_ref.task_id).with_for_update()
        )
        approval = db.scalar(
            select(ExecutionApproval).where(ExecutionApproval.id == approval_id).with_for_update()
        )
        if approval is None:
            raise HTTPException(status_code=404, detail="审批记录不存在")
        if approval.status != "approved":
            raise HTTPException(status_code=409, detail="只有已批准的审批可以撤销")
        if task is not None and task.status == "running":
            raise HTTPException(status_code=409, detail="任务已开始执行；请使用任务取消接口停止执行")
        approval.status, approval.decided_at = "revoked", utcnow()
        approval.decision_note = ((body or {}).get("note") or "")[:1000] or None
        db.add(TaskEvent(task_id=approval.task_id, event_type="execution.approval_revoked", payload={
            "approval_id": approval.id, "decision_source": "local_control_plane",
        }))
        db.commit()
        db.refresh(approval)
        return _payload(db, approval)
