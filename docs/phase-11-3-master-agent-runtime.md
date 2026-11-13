# Phase 11.3 — Master Agent Runtime

## Scope

Phase 11.3 is delivered in independently verifiable slices. This branch starts with **11.3.1: durable Claude Code turn execution**. It connects queued `AgentTurn` records to a dedicated runtime process, persists lifecycle/stream events and assistant replies, resumes a Claude Code session across turns, and supports cancellation and lease recovery.

It deliberately does not yet expose task/workflow creation tools to Claude Code or change the Dashboard. Those are 11.3.2 (restricted MCP control-plane tools) and 11.3.3 (chat UI/live event presentation).

## Runtime boundaries

- The API only records the user message, queued turn, and `turn.queued` event. It never launches Claude Code in a request handler.
- A separate `python -m ai_work_os.master_agent` process claims queued turns using a database lease and a conditional status update.
- A turn lease is fenced by worker ID and expiry. An expired in-flight turn is marked failed rather than replayed automatically: a duplicate Claude invocation could have side effects or duplicate a response.
- Claude Code runs in print mode with newline-delimited `stream-json` output and partial-message events. The session ID is persisted as soon as observed and passed back with `--resume` on later turns in the same conversation.
- The first slice is read-only: Claude starts in `plan` permission mode, MCP servers are explicitly disabled, and no task-dispatch tools are exposed.
- Project conversations use the project's configured workspace. Conversations without a project use `AI_WORK_OS_MASTER_WORKSPACE`; if unset, a dedicated storage-owned workspace is created.
- The CLI process is terminated on cancellation, timeout, or worker shutdown. The worker never uses `--dangerously-skip-permissions` or `bypassPermissions`.
- Claude session files live in a per-conversation `CLAUDE_CONFIG_DIR` under `AI_WORK_OS_STORAGE/master-agent/claude-config/`. This keeps resume state across worker restarts without exposing the user's real Claude home. With Bubblewrap, only this dedicated directory is bind-mounted at `/tmp/home/.claude`; it must remain outside the project workspace and task artifact directory.
- When Bubblewrap is enabled, the runtime must use the same fail-closed sandbox builder; it must never silently fall back to an unsandboxed process.

## Configuration

- `AI_WORK_OS_CLAUDE_BIN` (default `claude`)
- `AI_WORK_OS_CLAUDE_MODEL` (optional model override shared with the task Worker)
- `AI_WORK_OS_MASTER_WORKSPACE` (optional existing absolute directory for unprojected conversations)
- `AI_WORK_OS_MASTER_POLL_SECONDS` (default 1)
- `AI_WORK_OS_MASTER_TURN_TIMEOUT_SECONDS` (default 1800)
- `AI_WORK_OS_MASTER_TURN_LEASE_SECONDS` (default 60)
- `AI_WORK_OS_MASTER_HEARTBEAT_SECONDS` (default 10)
- `AI_WORK_OS_MASTER_MAX_TURNS` (default 12)

When Bubblewrap is enabled, the existing sandbox policy still applies. Its default network mode is `none`; Claude API access requires the operator to explicitly set `AI_WORK_OS_SANDBOX_NETWORK=host`, which permits outbound network access. The Master Agent does not weaken or bypass this policy.\n\nStart separately from the task Worker:

```bash
python -m ai_work_os.master_agent
```

## Acceptance

Automated tests cover atomic turn claiming, lease fencing/recovery, CLI argument construction, stream event parsing, assistant transcript persistence, session resume, cancellation, and process cleanup. A real-host acceptance on the supported Claude Code CLI version must verify the exact stream-json stdin/stdout contract, session ID persistence/resume, UTF-8/CJK round-trip, cancellation, and Bubblewrap fail-closed behavior before this runtime is enabled for normal use.

## Deferred slices

- **11.3.2:** restricted MCP tools for project/task/workflow inspection and creation. Tool arguments are untrusted input and must be validated against project/workspace policy before writes.
- **11.3.3:** chat UI, event polling/SSE, partial assistant rendering, turn status and cancellation controls.
