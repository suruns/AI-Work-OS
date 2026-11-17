from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from uuid import uuid4


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "src"))
    claude_bin = os.getenv("AI_WORK_OS_CLAUDE_BIN", "claude")
    if shutil.which(claude_bin) is None:
        print("FAIL: Claude Code CLI 不在 PATH 中")
        return 2
    if shutil.which(os.getenv("AI_WORK_OS_BWRAP_BIN", "bwrap")) is None:
        print("FAIL: Bubblewrap 不在 PATH 中")
        return 2
    if not os.getenv("AI_WORK_OS_CLAUDE_MODEL", "").strip():
        print("FAIL: 请设置 AI_WORK_OS_CLAUDE_MODEL 为当前网关可用的模型")
        return 2
    if os.getenv("AI_WORK_OS_SANDBOX_MODE", "").strip().lower() != "bubblewrap":
        print("FAIL: 请设置 AI_WORK_OS_SANDBOX_MODE=bubblewrap")
        return 2
    os.environ.setdefault("AI_WORK_OS_SANDBOX_NETWORK", "host")

    with tempfile.TemporaryDirectory(prefix="awos-controlled-execution-") as temp:
        temp_path = Path(temp)
        workspace = temp_path / "workspace"
        workspace.mkdir()
        storage = temp_path / "storage"
        storage.mkdir()
        db_path = temp_path / "awos.db"
        os.environ["AI_WORK_OS_DB"] = f"sqlite:///{db_path}"
        os.environ["AI_WORK_OS_STORAGE"] = str(storage)

        from sqlalchemy import select
        from fastapi.testclient import TestClient
        from ai_work_os.api import app
        from ai_work_os.db import Base, SessionLocal, engine
        from ai_work_os.models import ExecutionApproval, Project, Task, TaskEvent, utcnow
        from ai_work_os import worker

        Base.metadata.create_all(bind=engine)
        client = TestClient(app)
        with SessionLocal() as db:
            project = Project(
                name="Phase 11.5 受控执行验收",
                slug=f"controlled-execution-{uuid4().hex[:8]}",
                workspace_path=str(workspace),
                status="active",
            )
            db.add(project)
            db.commit()
            project_id = project.id

        def create_configured_task(title: str, prompt: str) -> dict:
            response = client.post("/tasks", json={
                "project_id": project_id,
                "title": title,
                "prompt": prompt,
                "workspace_path": str(workspace),
                "permission_mode": "configured",
                "max_attempts": 1,
                "acceptance_criteria": "只创建指定文件，不修改其他文件。",
                "verification_commands": [],
            })
            if response.status_code != 201:
                raise RuntimeError(f"创建任务失败：{response.status_code} {response.text}")
            return response.json()

        def request_approval(task_id: str) -> dict:
            response = client.post(f"/execution-approvals/request/{task_id}")
            if response.status_code != 200:
                raise RuntimeError(f"申请审批失败：{response.status_code} {response.text}")
            return response.json()

        worker_id = f"controlled-acceptance-{uuid4().hex[:8]}"

        # Pending and rejected tasks must remain unclaimable.
        rejected_task = create_configured_task("拒绝门禁测试", "不要执行任何操作。")
        rejected_approval = request_approval(rejected_task["id"])
        assert rejected_approval["status"] == "pending"
        assert worker.claim_task(worker_id) is None
        rejected = client.post(
            f"/execution-approvals/{rejected_approval['id']}/reject", json={"note": "验收：拒绝执行"}
        )
        assert rejected.status_code == 200
        assert worker.claim_task(worker_id) is None

        # A scope mutation after approval must invalidate the request.
        changed_task = create_configured_task("范围变更门禁测试", "原始任务范围。")
        changed_approval = request_approval(changed_task["id"])
        approved_changed = client.post(
            f"/execution-approvals/{changed_approval['id']}/approve", json={}
        )
        assert approved_changed.status_code == 200
        with SessionLocal() as db:
            record = db.get(Task, changed_task["id"])
            record.prompt = "已在审批后修改的任务范围。"
            db.commit()
        assert worker.claim_task(worker_id) is None
        changed_detail = client.get(f"/execution-approvals/{changed_approval['id']}").json()
        assert changed_detail["scope_valid"] is False

        # An expired approval must be recorded as expired and never claimed.
        expired_task = create_configured_task("过期门禁测试", "不要执行任何操作。")
        expired_approval = request_approval(expired_task["id"])
        with SessionLocal() as db:
            record = db.get(ExecutionApproval, expired_approval["id"])
            record.expires_at = utcnow() - timedelta(seconds=1)
            db.commit()
        assert worker.claim_task(worker_id) is None
        with SessionLocal() as db:
            assert db.get(ExecutionApproval, expired_approval["id"]).status == "expired"

        # Only the explicitly approved task may reach a real Claude Code process.
        target_name = "approval-proof.txt"
        target_content = "由已批准的 Worker 写入"
        execution_task = create_configured_task(
            "批准后真实写入测试",
            f"请使用 Claude Code 文件编辑工具，只在项目根目录创建 {target_name}。"
            f"文件内容必须严格为这一行：{target_content}。"
            "不要运行 Shell/Bash 命令，不要调用 MCP 工具，不要修改其他文件。"
            "完成后用简体中文简短报告。",
        )
        # Add a verification command before the approval digest is created.
        with SessionLocal() as db:
            record = db.get(Task, execution_task["id"])
            record.verification_commands = [f"test -f {target_name}"]
            record.verification_status = "pending"
            db.commit()
        execution_approval = request_approval(execution_task["id"])
        approved = client.post(
            f"/execution-approvals/{execution_approval['id']}/approve", json={"note": "本机真实 CLI + Bubblewrap 验收"}
        )
        if approved.status_code != 200 or not approved.json().get("scope_valid"):
            raise RuntimeError(f"批准失败或审批范围无效：{approved.status_code} {approved.text}")

        claimed = worker.claim_task(worker_id)
        if claimed != execution_task["id"]:
            raise RuntimeError(f"批准任务未被 Worker 领取：expected={execution_task['id']} actual={claimed}")
        asyncio.run(worker.execute_task(claimed, worker_id))

        with SessionLocal() as db:
            finished = db.get(Task, execution_task["id"])
            events = list(db.scalars(
                select(TaskEvent).where(TaskEvent.task_id == execution_task["id"]).order_by(TaskEvent.id)
            ))
        created_file = workspace / target_name
        if finished is None or finished.status != "succeeded":
            raise RuntimeError(
                f"Worker 执行未成功：status={finished.status if finished else 'missing'} "
                f"error={finished.error_summary if finished else ''}"
            )
        if finished.verification_status != "passed":
            raise RuntimeError(f"验证未通过：{finished.verification_status}")
        if not created_file.is_file() or created_file.read_text(encoding="utf-8").strip() != target_content:
            raise RuntimeError("批准任务没有生成预期文件内容")
        event_types = [event.event_type for event in events]
        start_event = next(
            (event for event in events if event.event_type == "executor.started"), None
        )
        command_text = str((start_event.payload or {}).get("command", "")) if start_event else ""
        approval_events = [
            event for event in events
            if event.event_type in {"execution.approval_requested", "execution.approval_approved"}
        ]
        if (
            start_event is None
            or (start_event.payload or {}).get("permission_mode") != "configured"
            or (start_event.payload or {}).get("mcp_policy") != "deny_all"
            or "acceptEdits" not in command_text
            or "Bash" not in command_text
            or "mcp__*" not in command_text
            or "bypassPermissions" in command_text
            or "--dangerously-skip-permissions" in command_text
        ):
            raise RuntimeError("Worker 执行命令未满足 acceptEdits / 禁止 Bash 与 MCP / 禁止 bypass 的策略")
        if any(
            {"prompt", "content", "api_key", "token", "secret"} & set(event.payload or {})
            for event in approval_events
        ):
            raise RuntimeError("审批审计事件包含不应记录的正文或密钥字段")
        required_events = {
            "execution.approval_requested", "execution.approval_approved",
            "executor.claimed", "executor.started", "verification.finished", "executor.finished",
        }
        missing = sorted(required_events - set(event_types))
        if missing:
            raise RuntimeError(f"缺少审批/执行审计事件：{missing}")
        if any(event.event_type == "execution.approval_invalid" for event in events):
            raise RuntimeError("批准执行期间意外触发审批失效事件")

        print("PASS: 受控执行审批闭环 + 真实 Claude Code + Bubblewrap 写入与验证")
        print("pending/rejected/scope_changed/expired=blocked")
        print(f"task_status={finished.status} verification={finished.verification_status}")
        print(f"created_file={created_file.name} audit_events={len(events)}")
        print("approval_audit=passed")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
