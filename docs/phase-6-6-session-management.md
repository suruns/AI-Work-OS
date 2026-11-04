# Phase 6.6 — Agent Session Management & Execution Control

## Scope

Phase 6.6 adds a session inventory and explicit execution-control capability reporting for the existing Claude Code CLI adapter. It reuses Task, TaskEvent, and the existing worker process lifecycle. It adds no session table, schema migration, dependency, or second execution engine.

## Session lifecycle

- The worker observes `session_id` from Claude Code `stream-json` events, rather than waiting for the final result event.
- The latest observed session ID is persisted on the Task row immediately, so a running task can appear in `GET /sessions`.
- Each invocation emits `agent.session.started` and, after the CLI process exits, `agent.session.ended` with the observed ID, worker, attempt, exit code, and terminal reason.
- Existing `claude.*` events remain the underlying source evidence. Task events retain earlier invocation IDs across retries; the Task row and session inventory expose the latest invocation ID.

## API

- `GET /sessions?limit=100`: list tasks with an observed session ID (limit 1–500).
- `GET /sessions/{session_id}`: return its task association and persisted lifecycle/control events.
- Existing `POST /tasks/{task_id}/cancel` remains the cancellation command. A running cancellation request records the API surface and action in `task.cancel_requested`.
- After the worker has completed the cancellation path and is finalizing the task, it emits `agent.control.acknowledged`. The event distinguishes a request from worker acknowledgement.

Session responses expose capabilities explicitly:
- `cancel`: available while the associated task is queued or running.
- `pause` / `resume`: always false for this adapter.

## Why pause/resume are unavailable

The current adapter launches one Claude Code CLI process per invocation and terminates its process group for cancellation. It has no verified suspend/resume contract that safely preserves tool execution, file writes, subprocesses, worker lease ownership, and CLI session continuation. Sending SIGSTOP/SIGCONT would only freeze processes; it would not establish a durable, resumable Agent session. The Dashboard therefore does not offer pause/resume controls.

The observed Claude Code session ID is useful for correlation and diagnostics; it is not treated as a guaranteed resume token.

## Audit and security boundaries

- Cancellation request and worker acknowledgement are separate persisted events.
- The current API has no authentication. `requested_by: api_client` identifies the API surface only; it is not a verified human identity. No claim of user-level attribution is made.
- Existing permission modes are unchanged. This phase does not widen Claude Code permissions or auto-approve tool actions.
- Worker lease renewal, orphan recovery, retry policy, and process-group termination remain authoritative and unchanged.

## Dashboard

Task detail shows the observed session ID and explicitly states that cancellation is the supported control while pause/resume are unavailable. The existing Cancel action remains task-scoped.

## Validation

The API tests cover session inventory/detail, capability flags, and cancellation request audit metadata. The existing API/unit and three-round Chromium Dashboard E2E CI suite remains required.
