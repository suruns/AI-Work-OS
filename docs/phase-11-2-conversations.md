# Phase 11.2 — Durable Conversations and Agent Turns

## Purpose

Phase 11.2 adds the persistence and API foundation for the chat-first Master Agent. It records conversations, transcript messages, queued agent turns, and append-only turn events. It does not launch Claude Code; execution is added in Phase 11.3.

## Data model

- `Conversation`: optional Project association, title, active/archived lifecycle, and timestamps.
- `ConversationMessage`: ordered transcript entry with role, content, metadata, and optional client-generated idempotency key.
- `AgentTurn`: one queued Master Agent execution request for a user message. It stores adapter identity, Claude session ID, status, timestamps, assistant message reference, and error summary.
- `AgentTurnEvent`: append-only structured event log for lifecycle transitions and later runtime output.

The transcript is the durable source of truth. A Claude CLI session ID is adapter metadata and is not a replacement for persisted messages.

## API

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/conversations` | Create a conversation, optionally linked to an active Project |
| GET | `/conversations` | List conversations, filter by project and status |
| GET | `/conversations/{id}` | Read conversation summary |
| PATCH | `/conversations/{id}` | Rename or archive/unarchive |
| GET | `/projects/{project_id}/conversations` | List project conversations |
| POST | `/conversations/{id}/messages` | Persist a user message and queue its AgentTurn |
| GET | `/conversations/{id}/messages?after_id=...` | Read transcript messages by cursor |
| GET | `/conversations/{id}/turns` | List turns for a conversation |
| GET | `/agent-turns/{id}` | Read one AgentTurn |
| GET | `/agent-turns/{id}/events?after_id=...` | Read append-only turn events by cursor |

## Reliability rules

1. Creating a user message, its queued AgentTurn, and the initial `turn.queued` event happens in one database transaction.
2. `client_message_id` is unique within a conversation. A repeated key returns the original message and turn rather than creating duplicates.
3. Idempotency lookup occurs before the archived-conversation check. Thus a retry of an already accepted message still returns its original result after the conversation is archived.
4. An archived conversation rejects new messages with HTTP 409.
5. An archived Project cannot receive new conversations.
6. Project summaries include counts of conversations grouped by status.
7. Both the API and standalone Worker import the models so `Base.metadata.create_all` registers the new tables in either process.

## Scope boundary

Phase 11.2 does not launch Claude Code, stream tokens, implement MCP control-plane tools, dispatch Tasks or Workflows, or provide the chat UI. Those capabilities are implemented in subsequent Phase 11 slices.
