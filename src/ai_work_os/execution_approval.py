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


def runtime_execution_policy() -> dict:
    """Return the effective process-wide policy that changes execution risk."""
    return {
        "claude_bin": os.getenv("AI_WORK_OS_CLAUDE_BIN", "claude").strip(),
        "claude_model": os.getenv("AI_WORK_OS_CLAUDE_MODEL", "").strip() or None,
        "claude_setting_sources": os.getenv("AI_WORK_OS_CLAUDE_SETTING_SOURCES", "project,local").strip(),
        "claude_config_dir": os.getenv("CLAUDE_CONFIG_DIR", "").strip() or None,
        "bwrap_bin": os.getenv("AI_WORK_OS_BWRAP_BIN", "bwrap").strip(),
        "sandbox_mode": os.getenv("AI_WORK_OS_SANDBOX_MODE", "off").strip().lower(),
        "sandbox_network": os.getenv("AI_WORK_OS_SANDBOX_NETWORK", "none").strip().lower(),
        "sandbox_runtime_paths": os.getenv("AI_WORK_OS_SANDBOX_RUNTIME_PATHS", ""),
    }


def claude_settings_fingerprints(workspace: Path) -> dict:
    """Bind approval to the settings files Claude will load for this task."""
    raw_sources = os.getenv("AI_WORK_OS_CLAUDE_SETTING_SOURCES", "project,local")
    sources = [item.strip() for item in raw_sources.split(",") if item.strip()]
    candidates = {}
    if "project" in sources:
        candidates["project"] = workspace / ".claude" / "settings.json"
    if "local" in sources:
        candidates["local"] = workspace / ".claude" / "settings.local.json"
    if "user" in sources:
        config_dir = Path(os.getenv("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")).expanduser()
        candidates["user"] = config_dir / "settings.json"

    fingerprints = {}
    for source, path in candidates.items():
        if not path.exists() and not path.is_symlink():
            fingerprints[source] = {"exists": False}
            continue
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise ValueError(f"Claude {source} settings path is not a regular file: {path}")
        content = resolved.read_bytes()
        fingerprints[source] = {
            "exists": True,
            "resolved_path": str(resolved),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    return fingerprints


def task_scope_hash(task: Task) -> str:
    workspace_path = Path(task.workspace_path).expanduser().resolve(strict=False)
    workspace = str(workspace_path)
    payload = {
        "claude_settings_fingerprints": claude_settings_fingerprints(workspace_path),
        "task_id": task.id, "project_id": task.project_id, "workspace_path": workspace,
        "title": task.title, "prompt_sha256": hashlib.sha256(task.prompt.encode("utf-8")).hexdigest(),
        "permission_mode": task.permission_mode, "acceptance_criteria": task.acceptance_criteria,
        "verification_commands": list(task.verification_commands or []),
        "auto_repair": bool(task.auto_repair), "max_repair_attempts": int(task.max_repair_attempts or 0),
        "max_attempts": int(task.max_attempts or 1),
        "runtime_execution_policy": runtime_execution_policy(),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()



def expire_approval_if_needed(db, task: Task, at=None) -> bool:
    """Persist the expiry transition and audit event while a queued task is examined."""
    if task.status != "queued":
        return False
    approval = db.scalar(
        select(ExecutionApproval)
        .where(ExecutionApproval.task_id == task.id)
        .with_for_update()
    )
    if approval is None or approval.status not in {"pending", "approved"}:
        return False
    expires = approval.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    timestamp = at or utcnow()
    if expires > timestamp:
        return False
    approval.status = "expired"
    approval.decided_at = timestamp
    db.add(TaskEvent(task_id=task.id, event_type="execution.approval_expired", payload={
        "approval_id": approval.id, "reason": "approval_ttl_elapsed",
    }))
    return True

def approval_is_valid(db, task: Task) -> bool:
    if not task.requires_approval:
        return task.permission_mode == "plan"
    approval = db.scalar(select(ExecutionApproval).where(ExecutionApproval.task_id == task.id).with_for_update())
    if approval is None or approval.status != "approved":
        return False
    expires = approval.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    project = db.scalar(select(Project).where(Project.id == task.project_id).with_for_update()) if task.project_id else None
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
    task_workspace = (
        str(Path(task.workspace_path).expanduser().resolve(strict=False))
        if task is not None else None
    )
    project_workspace = (
        str(Path(project.workspace_path).expanduser().resolve(strict=False))
        if project is not None else None
    )
    can_resubmit = bool(
        task is not None and task.status == "queued"
        and task.permission_mode == "configured"
        and project is not None and project.status == "active"
        and task_workspace == project_workspace
    )
    effective_status = approval.status
    expires = approval.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if effective_status == "pending" and (task is None or task.status != "queued"):
        effective_status = "revoked"
    elif effective_status == "approved" and (task is None or task.status not in {"queued", "running"}):
        effective_status = "revoked"
    elif effective_status in {"pending", "approved"} and task is not None and task.status == "queued" and expires <= utcnow():
        effective_status = "expired"
    return {
        "id": approval.id, "task_id": approval.task_id, "project_id": approval.project_id,
        "project_name": project.name if project else None, "task_title": task.title if task else None, "task_status": task.status if task else None,
        "workspace_path": approval.workspace_path, "permission_mode": task.permission_mode if task else None,
        "acceptance_criteria": task.acceptance_criteria if task else None,
        "prompt": task.prompt if task else None,
        "verification_commands": list(task.verification_commands or []) if task else [],
        "max_attempts": task.max_attempts if task else None,
        "max_repair_attempts": task.max_repair_attempts if task else None,
        "auto_repair": bool(task.auto_repair) if task else False,
        "status": effective_status,
        "requested_at": approval.requested_at, "decided_at": approval.decided_at,
        "expires_at": approval.expires_at, "decision_source": approval.decision_source,
        "decision_note": approval.decision_note,
        "runtime_policy": runtime_execution_policy(),
        "can_resubmit": can_resubmit,
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
        if db.bind is not None and db.bind.dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task.status != "queued" or task.permission_mode != "configured" or not task.project_id:
            raise HTTPException(status_code=409, detail="只有绑定项目且排队中的受控写入任务可以申请审批")
        existing = db.scalar(
            select(ExecutionApproval)
            .where(ExecutionApproval.task_id == task.id)
            .with_for_update()
        )
        project = db.scalar(select(Project).where(Project.id == task.project_id).with_for_update())
        if project is None or project.status != "active":
            raise HTTPException(status_code=409, detail="项目不存在或已归档")
        workspace = str(Path(task.workspace_path).expanduser().resolve(strict=False))
        project_workspace = str(Path(project.workspace_path).expanduser().resolve(strict=False))
        if workspace != project_workspace:
            raise HTTPException(status_code=409, detail="任务工作区与项目工作区不一致")

        scope_hash = task_scope_hash(task)
        now = utcnow()
        if existing is not None:
            expires = existing.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if (
                existing.scope_hash == scope_hash
                and existing.workspace_path == workspace
                and existing.status == "pending"
                and expires > now
            ):
                return _payload(db, existing)
            if (
                existing.status == "approved"
                and existing.scope_hash == scope_hash
                and approval_is_valid(db, task)
            ):
                return _payload(db, existing)

            previous_status = existing.status
            if previous_status in {"pending", "approved"} and expires <= now:
                existing.status = "expired"
                existing.decided_at = now
                db.add(TaskEvent(task_id=task.id, event_type="execution.approval_expired", payload={
                    "approval_id": existing.id, "reason": "expired_before_resubmission",
                }))
            existing.status = "pending"
            existing.project_id = project.id
            existing.workspace_path = workspace
            existing.scope_hash = scope_hash
            existing.requested_at = now
            existing.decided_at = None
            existing.expires_at = now + timedelta(hours=DEFAULT_TTL_HOURS)
            existing.decision_source = None
            existing.decision_note = None
            task.requires_approval = True
            db.add(TaskEvent(task_id=task.id, event_type="execution.approval_requested", payload={
                "approval_id": existing.id, "project_id": project.id,
                "permission_mode": task.permission_mode, "scope_hash": scope_hash,
                "expires_at": existing.expires_at.isoformat(),
                "resubmission": True, "previous_status": previous_status,
            }))
            db.commit()
            db.refresh(existing)
            return _payload(db, existing)

        approval = ExecutionApproval(
            task_id=task.id, project_id=project.id, workspace_path=workspace,
            scope_hash=scope_hash, status="pending",
            expires_at=now + timedelta(hours=DEFAULT_TTL_HOURS),
        )
        task.requires_approval = True
        db.add(approval)
        db.flush()
        db.add(TaskEvent(task_id=task.id, event_type="execution.approval_requested", payload={
            "approval_id": approval.id, "project_id": project.id,
            "permission_mode": task.permission_mode, "scope_hash": approval.scope_hash,
            "expires_at": approval.expires_at.isoformat(),
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
    if status and status not in {"pending", "approved", "rejected", "revoked", "expired"}:
        raise HTTPException(status_code=400, detail="无效的审批状态")
    with SessionLocal() as db:
        records = list(db.scalars(
            select(ExecutionApproval).order_by(ExecutionApproval.requested_at.desc())
        ))
        payloads = [_payload(db, record) for record in records]
        if status:
            payloads = [item for item in payloads if item["status"] == status]
        return {"approvals": payloads}


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
            db.add(TaskEvent(task_id=approval.task_id, event_type="execution.approval_expired", payload={
                "approval_id": approval.id, "reason": "expired_at_decision",
            }))
            db.commit()
            raise HTTPException(status_code=409, detail="审批已过期")
        if task is None or task.status != "queued" or not task.requires_approval:
            raise HTTPException(status_code=409, detail="任务已不处于可审批状态")
        project = db.scalar(select(Project).where(Project.id == approval.project_id).with_for_update())
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
