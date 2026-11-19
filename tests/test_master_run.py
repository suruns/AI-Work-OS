from ai_work_os.master_run import derive_run_status
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from ai_work_os import master_run as run_service
from ai_work_os.db import Base
from ai_work_os.models import Conversation, MasterRun, MasterRunTask, Project, Task, TaskEvent, utcnow


def _task(status, *, verification="passed", approval=None, required=True):
    return {
        "status": status,
        "verification_status": verification,
        "approval_status": approval,
        "required": required,
    }


def test_queued_tasks_are_ready_not_running():
    assert derive_run_status([_task("queued")]) == "ready"


def test_running_task_keeps_goal_running():
    assert derive_run_status([_task("running")]) == "running"


def test_pending_approval_takes_precedence_over_queued_state():
    assert derive_run_status([
        _task("queued", verification="pending", approval="pending"),
    ]) == "waiting_approval"


def test_failed_required_task_blocks_goal():
    assert derive_run_status([_task("failed")]) == "blocked"


def test_failed_optional_task_does_not_block_success():
    assert derive_run_status([
        _task("succeeded"),
        _task("failed", required=False),
    ]) == "succeeded"


def test_cancelled_required_task_cancels_goal():
    assert derive_run_status([_task("cancelled")]) == "cancelled"


def test_success_without_verification_evidence_is_not_success():
    assert derive_run_status([
        _task("succeeded", verification="not_configured"),
    ]) == "verifying"


def test_verification_failure_blocks_goal():
    assert derive_run_status([
        _task("succeeded", verification="failed"),
    ]) == "blocked"


def test_success_requires_passed_verification_for_every_required_task():
    assert derive_run_status([
        _task("succeeded", verification="passed"),
        _task("succeeded", verification="passed"),
    ]) == "succeeded"


def test_no_required_tasks_remains_in_planning():
    assert derive_run_status([_task("failed", required=False)]) == "planning"


def test_rejected_approval_blocks_goal():
    assert derive_run_status([
        _task("queued", verification="pending", approval="rejected"),
    ]) == "blocked"


def test_revoked_approval_blocks_goal():
    assert derive_run_status([
        _task("queued", verification="pending", approval="revoked"),
    ]) == "blocked"


def test_goal_is_not_reported_succeeded_until_master_run_terminal_transition():
    from ai_work_os.master_run import reported_run_status
    from ai_work_os.models import MasterRun

    rows = [_task("succeeded", verification="passed")]
    run = MasterRun(status="running")
    assert reported_run_status(run, rows) == "verifying"
    run.status = "succeeded"
    assert reported_run_status(run, rows) == "succeeded"
    run.status = "failed"
    assert reported_run_status(run, rows) == "failed"


def test_master_run_success_requires_persisted_verification_evidence(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(run_service, "SessionLocal", factory)
    try:
        with factory() as db:
            project = Project(
                name="Evidence project", slug="evidence-project",
                workspace_path=str(tmp_path), status="active",
            )
            db.add(project)
            db.flush()
            conversation = Conversation(project_id=project.id, title="Evidence")
            db.add(conversation)
            db.flush()
            run = MasterRun(
                conversation_id=conversation.id, project_id=project.id,
                goal="完成并验证", status="running", active_key=conversation.id,
                plan={}, checkpoint={}, step_count=1, max_steps=20,
            )
            task = Task(
                project_id=project.id, title="实现", prompt="实现功能",
                workspace_path=str(tmp_path), permission_mode="plan",
                status="succeeded", verification_status="not_configured",
            )
            db.add_all([run, task])
            db.flush()
            db.add(MasterRunTask(master_run_id=run.id, task_id=task.id, required=True))
            db.commit()
            run_id, conversation_id, project_id, task_id = (
                run.id, conversation.id, project.id, task.id,
            )
        with pytest.raises(run_service.MasterRunError, match="验证通过"):
            run_service.transition_run(
                run_id, conversation_id, project_id, "succeeded",
            )
        with pytest.raises(run_service.MasterRunError, match="cancel_goal"):
            run_service.transition_run(
                run_id, conversation_id, project_id, "cancelled",
            )
        with factory() as db:
            db.get(Task, task_id).verification_status = "passed"
            db.commit()
        result = run_service.transition_run(
            run_id, conversation_id, project_id, "succeeded",
        )
        assert result["status"] == "succeeded"
        with factory() as db:
            assert db.get(MasterRun, run_id).active_key is None
    finally:
        engine.dispose()


def test_budget_extension_resumes_a_blocked_goal_only_within_limits(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(run_service, "SessionLocal", factory)
    try:
        with factory() as db:
            project = Project(
                name="Budget project", slug="budget-project",
                workspace_path=str(tmp_path), status="active",
            )
            db.add(project)
            db.flush()
            conversation = Conversation(project_id=project.id, title="Budget")
            db.add(conversation)
            db.flush()
            run = MasterRun(
                conversation_id=conversation.id, project_id=project.id,
                goal="受限目标", status="blocked", active_key=conversation.id,
                plan={}, checkpoint={"stop_reason": "step_budget_exceeded", "continuation_signature": "stable-signature"},
                step_count=20, max_steps=20, deadline_at=utcnow(),
            )
            db.add(run)
            db.commit()
            run_id, conversation_id, project_id = run.id, conversation.id, project.id
        with pytest.raises(run_service.MasterRunError, match="必须明确提高 max_steps"):
            run_service.extend_run_budget(
                run_id, conversation_id, project_id, deadline_hours=2,
            )
        result = run_service.extend_run_budget(
            run_id, conversation_id, project_id,
            max_steps=30, deadline_hours=2,
        )
        assert result["status"] == "running"
        assert result["max_steps"] == 30
        assert "stop_reason" not in result["checkpoint"]
        updated = run_service.update_plan_checkpoint(
            run_id, conversation_id, project_id, checkpoint={"last_step": "inspect"},
        )
        assert updated["checkpoint"]["continuation_signature"] == "stable-signature"
        assert updated["checkpoint"]["last_step"] == "inspect"
        with pytest.raises(run_service.MasterRunError, match="新的 max_steps"):
            run_service.extend_run_budget(
                run_id, conversation_id, project_id, max_steps=30,
            )
    finally:
        engine.dispose()


def test_expired_pending_approval_status_handles_naive_sqlite_timestamp():
    from datetime import timedelta
    from ai_work_os.master_run import _effective_approval_status
    from ai_work_os.models import utcnow

    expired = (utcnow() - timedelta(seconds=1)).replace(tzinfo=None)
    assert _effective_approval_status("pending", expired) == "expired"


def test_goal_task_evidence_is_summarized_without_raw_command_output(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(run_service, "SessionLocal", factory)
    try:
        with factory() as db:
            project = Project(
                name="Evidence summary project", slug="evidence-summary-project",
                workspace_path=str(tmp_path), status="active",
            )
            db.add(project)
            db.flush()
            conversation = Conversation(project_id=project.id, title="Evidence summary")
            db.add(conversation)
            db.flush()
            run = MasterRun(
                conversation_id=conversation.id, project_id=project.id,
                goal="生成证据摘要", status="running", active_key=conversation.id,
                plan={}, checkpoint={}, step_count=1, max_steps=20,
            )
            task = Task(
                project_id=project.id, title="验证任务", prompt="执行验证",
                workspace_path=str(tmp_path), permission_mode="plan",
                status="succeeded", verification_status="passed",
                verification_commands=["python -m pytest -q"],
            )
            db.add_all([run, task])
            db.flush()
            db.add(MasterRunTask(master_run_id=run.id, task_id=task.id, required=True))
            db.add_all([
                TaskEvent(task_id=task.id, event_type="verification.finished", payload={
                    "status": "passed",
                    "commands": [{"command": "python -m pytest -q", "exit_code": 0,
                                  "timed_out": False, "stdout": "secret raw output"}],
                }),
                TaskEvent(task_id=task.id, event_type="workspace.changes_reported", payload={
                    "available": True, "changed_file_count": 2,
                    "report_path": "/tmp/workspace-changes.json",
                    "report_written": True, "preexisting_file_count": 0,
                }),
            ])
            db.commit()
            run_id, conversation_id, project_id = run.id, conversation.id, project.id
        rows = run_service.list_run_tasks(run_id, conversation_id, project_id)
        evidence = rows[0]["evidence"]
        assert evidence["verification"] == {
            "status": "passed", "command_count": 1, "passed_count": 1, "failed_count": 0,
        }
        assert evidence["workspace_changes"]["changed_file_count"] == 2
        assert "stdout" not in str(evidence)
    finally:
        engine.dispose()
