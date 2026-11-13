from datetime import timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fastapi.testclient import TestClient

from ai_work_os import api as api_module
from ai_work_os import master_agent
from ai_work_os.db import Base
from ai_work_os.models import AgentTurn, AgentTurnEvent, Conversation, ConversationMessage


def isolated_db(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(master_agent, "SessionLocal", factory)
    return engine, factory


def queued_turn(factory):
    with factory() as db:
        conversation = Conversation(title="Runtime test", status="active")
        db.add(conversation)
        db.flush()
        message = ConversationMessage(
            conversation_id=conversation.id, role="user", content="Summarize this project",
            message_metadata={},
        )
        db.add(message)
        db.flush()
        turn = AgentTurn(
            conversation_id=conversation.id, user_message_id=message.id,
            status="queued", adapter="claude-code",
        )
        db.add(turn)
        db.commit()
        return turn.id, conversation.id


def test_master_command_is_read_only_and_resumes_session(tmp_path, monkeypatch):
    monkeypatch.setattr(master_agent, "MASTER_MODEL", "claude-test-model")
    command = master_agent.build_master_command("session-123", tmp_path / "mcp.json")
    assert command[0] == master_agent.CLAUDE_BIN
    assert "--output-format" in command
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert "--include-partial-messages" in command
    assert command[command.index("--permission-mode") + 1] == "plan"
    assert command[command.index("--tools") + 1] == "Read,Glob,Grep"
    assert command[command.index("--resume") + 1] == "session-123"
    assert command[command.index("--model") + 1] == "claude-test-model"
    assert command[command.index("--strict-mcp-config") + 1] == "--mcp-config"
    assert command[command.index("--disallowedTools") + 1] == "mcp__*"
    denied = command[command.index("--disallowedTools") + 1:command.index("--max-turns")]
    assert {"mcp__*", "Bash", "Edit", "Write", "NotebookEdit"} <= set(denied)
    assert "--dangerously-skip-permissions" not in command


def test_extract_final_text_from_result_and_assistant_events():
    assert master_agent._extract_text({"type": "result", "result": "Final answer"}) == "Final answer"
    assert master_agent._extract_text({"type": "result", "result": "failure", "is_error": True}) is None
    event = {
        "type": "assistant",
        "message": {
            "stop_reason": "end_turn",
            "content": [
                {"type": "text", "text": "Line one"},
                {"type": "tool_use", "id": "tool-1"},
                {"type": "text", "text": "Line two"},
            ],
        },
    }
    assert master_agent._extract_text(event) == "Line one\nLine two"
    assert master_agent._extract_text({
        "type": "assistant",
        "message": {"stop_reason": "tool_use", "content": [{"type": "text", "text": "not final"}]},
    }) is None


def test_claim_turn_persists_worker_lease_and_event(monkeypatch):
    engine, factory = isolated_db(monkeypatch)
    try:
        turn_id, _ = queued_turn(factory)
        claimed = master_agent._claim_turn("worker-test")
        assert claimed == turn_id
        with factory() as db:
            turn = db.get(AgentTurn, turn_id)
            assert turn.status == "running"
            assert turn.worker_id == "worker-test"
            assert turn.lease_expires_at is not None
            events = list(db.scalars(select(AgentTurnEvent).where(AgentTurnEvent.turn_id == turn_id)))
            assert [event.event_type for event in events] == ["turn.started"]
    finally:
        engine.dispose()


def test_expired_turn_lease_is_failed_not_replayed(monkeypatch):
    engine, factory = isolated_db(monkeypatch)
    try:
        turn_id, _ = queued_turn(factory)
        assert master_agent._claim_turn("worker-test") == turn_id
        with factory() as db:
            turn = db.get(AgentTurn, turn_id)
            turn.lease_expires_at = master_agent.now() - timedelta(seconds=1)
            db.commit()
        assert master_agent.recover_orphaned_turns() == 1
        with factory() as db:
            turn = db.get(AgentTurn, turn_id)
            assert turn.status == "failed"
            assert turn.worker_id is None
            assert turn.lease_expires_at is None
            assert any(
                event.event_type == "turn.recovered"
                for event in db.scalars(select(AgentTurnEvent).where(AgentTurnEvent.turn_id == turn_id))
            )
    finally:
        engine.dispose()


def test_finish_turn_persists_assistant_message_atomically(monkeypatch):
    engine, factory = isolated_db(monkeypatch)
    try:
        turn_id, conversation_id = queued_turn(factory)
        assert master_agent._claim_turn("worker-test") == turn_id
        master_agent._finish_turn(
            turn_id, "worker-test", "succeeded",
            assistant_text="The project is healthy.", exit_code=0,
        )
        with factory() as db:
            turn = db.get(AgentTurn, turn_id)
            assert turn.status == "succeeded"
            assert turn.assistant_message_id is not None
            message = db.get(ConversationMessage, turn.assistant_message_id)
            assert message.conversation_id == conversation_id
            assert message.role == "assistant"
            assert message.content == "The project is healthy."
            assert message.message_metadata["turn_id"] == turn_id
    finally:
        engine.dispose()



def test_empty_mcp_config_is_valid_and_private(tmp_path):
    path = master_agent._write_empty_mcp_config(tmp_path)
    assert path.read_text(encoding="utf-8") == '{"mcpServers":{}}\n'
    assert path.stat().st_mode & 0o777 == 0o600


def test_cancel_queued_turn_is_terminal_and_audited(monkeypatch):
    engine, factory = isolated_db(monkeypatch)
    monkeypatch.setattr(api_module, "SessionLocal", factory)
    try:
        turn_id, _ = queued_turn(factory)
        response = TestClient(api_module.app).post(f"/agent-turns/{turn_id}/cancel")
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"
        assert response.json()["cancel_requested"] is True
        with factory() as db:
            events = list(db.scalars(select(AgentTurnEvent).where(AgentTurnEvent.turn_id == turn_id)))
            assert [event.event_type for event in events] == ["turn.cancelled"]
    finally:
        engine.dispose()


def test_cancel_running_turn_records_request_without_stealing_lease(monkeypatch):
    engine, factory = isolated_db(monkeypatch)
    monkeypatch.setattr(api_module, "SessionLocal", factory)
    try:
        turn_id, _ = queued_turn(factory)
        assert master_agent._claim_turn("worker-running") == turn_id
        response = TestClient(api_module.app).post(f"/agent-turns/{turn_id}/cancel")
        assert response.status_code == 200
        assert response.json()["status"] == "running"
        assert response.json()["cancel_requested"] is True
        with factory() as db:
            turn = db.get(AgentTurn, turn_id)
            assert turn.worker_id == "worker-running"
            assert turn.lease_expires_at is not None
            events = list(db.scalars(select(AgentTurnEvent).where(AgentTurnEvent.turn_id == turn_id)))
            assert [event.event_type for event in events] == [
                "turn.started", "turn.cancel_requested",
            ]
    finally:
        engine.dispose()


def test_finish_turn_cannot_overwrite_an_expired_worker_lease(monkeypatch):
    engine, factory = isolated_db(monkeypatch)
    try:
        turn_id, _ = queued_turn(factory)
        assert master_agent._claim_turn("stale-worker") == turn_id
        with factory() as db:
            turn = db.get(AgentTurn, turn_id)
            turn.lease_expires_at = master_agent.now() - timedelta(seconds=1)
            db.commit()
        master_agent._finish_turn(
            turn_id, "stale-worker", "succeeded",
            assistant_text="This stale result must not be committed.", exit_code=0,
        )
        with factory() as db:
            turn = db.get(AgentTurn, turn_id)
            assert turn.status == "running"
            assert turn.assistant_message_id is None
            messages = list(db.scalars(
                select(ConversationMessage).where(
                    ConversationMessage.conversation_id == turn.conversation_id,
                    ConversationMessage.role == "assistant",
                )
            ))
            assert messages == []
        assert master_agent.recover_orphaned_turns() == 1
        with factory() as db:
            assert db.get(AgentTurn, turn_id).status == "failed"
    finally:
        engine.dispose()
