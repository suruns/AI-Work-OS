# Phase 11.2 — Durable conversations and Agent Turns

## Status

Implementation in progress on `feat/phase-11-2-conversations`, stacked on Phase 11.1. This phase adds the persistence and API contract for the chat-first experience. It does not yet launch Claude Code, stream tokens, or dispatch tasks.

## Data model

- `Conversation`: optional project association, title, active/archived lifecycle, timestamps.
- `ConversationMessage`: append-only user/assistant/tool message record, content, metadata, timestamp, and optional client-generated idempotency key.
- `AgentTurn`: one orchestrated response attempt tied to exactly one user message; status, adapter, Claude CLI session ID, timestamps, optional assistant message, and error summary.
- `AgentTurnEvent`: append-only structured lifecycle/stream event log for replay and diagnostics.

## API behavior

- Create/list/detail/update conversations.
- Filter conversations by project and active/archived status.
- Create a user message and queued AgentTurn in one database transaction.
- A `client_message_id` is unique within a conversation. Repeated sends with the same key return the existing message and turn instead of enqueuing duplicate work.
- First user message automatically supplies a short default conversation title.
- Read transcript messages with an increasing integer cursor (`after_id`).
- Read AgentTurn state and its ordered event log with an increasing cursor.
- Archived conversations reject new messages. Archived projects cannot receive new conversations.

## Reliability invariants

- The user message, queued AgentTurn, and initial `turn.queued` event commit atomically.
- A conversation message is never silently converted into a task; the AgentTurn is the explicit handoff point for the Master Agent runtime.
- Conversation history is the durable source of truth. Claude CLI session IDs are adapter metadata, not the only source of continuity.
- Event IDs are monotonic within the database and can be used by clients to replay events after reconnect.
- Client retries are idempotent when the same `client_message_id` is supplied.
- Existing Task/Workflow scheduling, Worker execution, artifacts, and sandbox policy are unchanged.

## Deliberately deferred

- Claude Code process adapter, bidirectional stream-json, partial token streaming, MCP control-plane tools, and session resume.
- AgentTurn claim/lease/recovery and cancellation lifecycle.
- Assistant/tool message persistence from actual CLI events.
- Browser chat UI and real-time transport (SSE/WebSocket).
- Task/Workflow dispatch tools and project-aware planning.

These belong to Phase 11.3 and later, after the operator validates the CLI protocol on the installed Claude Code 2.1.306 host.
