# Phase 11.4 — Chat-first Master Agent UI

## Goal

Make the persistent Master Agent conversation the default application home while preserving the existing operational task dashboard at /dashboard.

## User flow

1. Open / and see the conversation workspace.
2. Select an active project or choose a general conversation with no project context.
3. Create a conversation and send a message.
4. The API atomically persists the user message, queued AgentTurn, and turn.queued event.
5. The UI polls the durable transcript, turn state, and persisted event log. It does not depend on an in-memory stream connection, so a reload can recover the same conversation and activity history.
6. A queued or running turn can be cancelled through the existing AgentTurn cancellation API.

## Scope and trust boundaries

- The UI uses existing conversation, message, turn, event, project, and cancellation APIs.
- Project selection is conversation context, not an authorization boundary. The API and Master Agent continue to enforce trusted project binding.
- Project MCP operations remain queue-only. The UI must not imply that creating a task/workflow starts a Task Worker.
- No new Claude CLI permissions, MCP tools, Worker behavior, sandbox mounts, or database schema are introduced.
- The previous task/workflow dashboard remains available at /dashboard.

## Live activity model

This slice uses short-interval polling of persisted records rather than introducing SSE/WebSocket infrastructure. The transcript remains the source of truth. Activity events are shown separately from assistant messages, with the original payload available for inspection. Polling is deliberately recoverable after browser reload and does not claim token-level streaming.

## Acceptance

- Root route serves the chat-first interface; /dashboard serves the existing dashboard.
- Create and select project-scoped and projectless conversations.
- Send messages with a client idempotency key.
- Render persisted user/assistant messages and AgentTurn states.
- Render persisted turn events and recover them after reload.
- Cancel queued/running turns using the existing endpoint.
- Existing Dashboard E2E tests continue to run against /dashboard.
- No Task Worker is started by chat message submission or MCP queue operations.
