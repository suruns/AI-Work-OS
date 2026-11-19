from datetime import timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ai_work_os import master_agent
from ai_work_os.db import Base
from ai_work_os.models import (
    AgentTurn, AgentTurnEvent, Conversation, ConversationMessage,
    ExecutionApproval, MasterRun, MasterRunTask, Project, Task, utcnow,
)


def _isolated(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(master_agent, "SessionLocal", factory)
    with factory() as db:
        project = Project(
            name="Continuation project", slug="continuation-project",
            workspace_path=str(tmp_path), status="active",
        )
        db.add(project)
        db.flush()
        conversation = Conversation(project_id=project.id, title="Goal continuation")
        db.add(conversation)
        db.flush()
        run = MasterRun(
            conversation_id=conversation.id, project_id=project.id,
            goal="完成并验证功能", status="running", active_key=conversation.id,
            plan={}, checkpoint={}, step_count=1, max_steps=20,
            deadline_at=utcnow() + timedelta(hours=2),
        )
        db.add(run)
        db.flush()
        task = Task(
            project_id=project.id, title="实现功能", prompt="实现并验证",
            workspace_path=str(tmp_path), permission_mode="plan",
            status="succeeded", verification_status="passed",
            verification_commands=["python -m pytest -q"],
        )
        db.add(task)
        db.flush()
        db.add(MasterRunTask(master_run_id=run.id, task_id=task.id, required=True))
        db.commit()
        ids = (run.id, conversation.id, task.id, project.id)
    return engine, factory, ids


def test_terminal_goal_tasks_enqueue_one_hidden_continuation(monkeypatch, tmp_path):
    engine, factory, (run_id, conversation_id, task_id, _) = _isolated(monkeypatch, tmp_path)
    try:
        assert master_agent.enqueue_goal_continuations() == 1
        assert master_agent.enqueue_goal_continuations() == 0
        with factory() as db:
            run = db.get(MasterRun, run_id)
            turns = list(db.scalars(select(AgentTurn).where(
                AgentTurn.conversation_id == conversation_id,
            )))
            assert len(turns) == 1
            turn = turns[0]
            message = db.get(ConversationMessage, turn.user_message_id)
            assert turn.status == "queued"
            assert message.message_metadata["internal"] is True
            assert message.message_metadata["kind"] == "master_run_continuation"
            assert message.message_metadata["master_run_id"] == run_id
            assert run.checkpoint["continuation_turn_id"] == turn.id
            event = db.scalar(select(AgentTurnEvent).where(
                AgentTurnEvent.turn_id == turn.id,
                AgentTurnEvent.event_type == "turn.queued",
            ))
            assert event is not None
    finally:
        engine.dispose()


def test_pending_approval_prevents_automatic_goal_continuation(monkeypatch, tmp_path):
    engine, factory, (run_id, _, task_id, project_id) = _isolated(monkeypatch, tmp_path)
    try:
        with factory() as db:
            task = db.get(Task, task_id)
            db.add(ExecutionApproval(
                task_id=task.id, project_id=project_id,
                workspace_path=task.workspace_path, scope_hash="a" * 64,
                status="pending", expires_at=utcnow() + timedelta(hours=1),
            ))
            db.commit()
        assert master_agent.enqueue_goal_continuations() == 0
        with factory() as db:
            assert db.scalar(select(AgentTurn.id)) is None
            assert db.get(MasterRun, run_id).checkpoint == {}
    finally:
        engine.dispose()
