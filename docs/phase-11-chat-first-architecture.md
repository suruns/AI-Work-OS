# Phase 11 — Chat-first Agent Orchestration & Project Workspace

## Status

Architecture proposal based on the merged `main` at `70b3088b377485cf73b6b6d00877dbb5ba8f1be9`. This document defines the target product and implementation sequence; it does not claim that the chat, project, or master-agent capabilities are implemented.

## Product direction

AI Work OS is a project-aware AI work platform, not a task dashboard with a chat box attached.

The primary interaction is a persistent conversation with a **Master Agent**. The user describes an outcome in natural language. The Master Agent understands the request, gathers project context, proposes or creates a plan, dispatches work through the existing Workflow/Task scheduler, monitors execution, and explains the final result. Users can interrupt, clarify, approve sensitive actions, or ask for follow-up changes in the same conversation.

Project management is a separate first-class navigation area. It provides a durable view of projects and their associated workflows, tasks, agent executions, and artifacts. The chat is the default home; project/task monitoring remains directly accessible and is not hidden inside chat.

### Primary navigation

1. **AI Assistant** — default landing page; conversation list and Master Agent chat.
2. **Projects** — project directory and project detail pages.
3. **Task Center** — cross-project task and workflow inventory.
4. **Execution Monitor** — worker liveness, active runs, events, failures, and cancellation.
5. **Files & Artifacts** — project/task artifact discovery and downloads.
6. **Settings** — Master Agent, Worker, permissions, sandbox, and runtime configuration.

### Project detail

A project page should provide:
- Overview: name, description, workspace root, status, recent activity, and progress.
- Tasks: all standalone tasks and workflow member tasks associated with the project.
- Workflows: DAGs, dependencies, aggregate status, and blocked reasons.
- Agent activity: Master Agent turns and Worker execution activity relevant to the project.
- Files & artifacts: project-linked outputs and task artifacts.
- Conversations: conversations explicitly associated with the project.

Progress is a transparent count/status summary, not a guessed percentage. A percentage may be shown only when its denominator and inclusion rules are visible.

## Core interaction

### Chat-first flow

1. User opens AI Assistant or enters a project and starts/continues a conversation.
2. The API persists the user message and creates a durable AgentTurn before execution.
3. The Master Agent receives the conversation transcript, project context, current task/workflow state, and a narrowly scoped set of control-plane tools.
4. The Master Agent may ask clarifying questions, provide a plan, or invoke tools to create/inspect/cancel work.
5. Task creation uses the existing `POST /workflows` and `POST /tasks` semantics, extended with project association. The existing Worker pool remains responsible for task execution.
6. The UI streams the Master Agent response and tool activity. Task/Workflow events remain the source of truth for execution progress.
7. The Master Agent can inspect task results/events/artifacts and report outcomes in the same conversation.
8. The user can continue the conversation; a follow-up turn receives the persisted transcript and current project state.

### Master Agent is not a Worker

- Master Agent owns conversation-level intent understanding, planning, dispatch decisions, progress interpretation, and user-facing synthesis.
- Workers execute bounded Task records. They do not own the user conversation and do not directly decide project scope.
- Workflows remain the durable dependency graph and scheduling unit.
- The Master Agent must use the existing scheduler/API rather than launching ad-hoc background processes or directly mutating task database rows.
- Master Agent failures must not corrupt or silently change already-created Tasks/Workflows.

### Planning and execution consent

The Master Agent should distinguish:
- **Clarification**: missing project, workspace, scope, acceptance criteria, or destructive-operation intent.
- **Plan proposal**: a human-readable summary of the intended workflow and its task breakdown.
- **Dispatch**: creating durable tasks/workflows once the user has asked it to do the work and required execution settings are known.
- **Approval-required action**: actions outside the configured policy, such as destructive operations, external publication, credential changes, or widening sandbox/network permissions.

Do not treat a model-generated plan as an authorization to bypass existing permission modes, sandbox policy, or user approval. The Master Agent cannot silently change a project's workspace path, permission mode, network policy, or sandbox mode.

## Target architecture

```
Browser
  ├── AI Assistant (conversation list, transcript, streaming composer)
  ├── Projects (project list and project detail)
  ├── Task Center / Execution Monitor / Files
  └── HTTP API
        ├── Conversation API ──> SQLite: conversations, messages, agent turns/events
        ├── Project API ───────> SQLite: projects
        ├── Agent Orchestrator
        │     ├── Master Agent adapter (Claude Code CLI, isolated role/config)
        │     ├── scoped control-plane tools
        │     └── Task/Workflow API client
        └── Existing Task/Workflow API ──> SQLite
                                         └── Existing Worker pool
                                               └── Claude Code task execution
```

The Master Agent adapter and task Worker adapter are separate trust and lifecycle roles. Phase 8's empty MCP configuration and Phase 10.1 sandbox policy for task execution must remain intact. The Master Agent can use a dedicated local MCP control-plane bridge configured with `--strict-mcp-config --mcp-config <master-agent-config>`; that bridge must expose only explicitly allowlisted AI Work OS operations. It must never be mounted into or reused by task Worker invocations, whose MCP config remains empty.

### Claude Code CLI feasibility findings

The current official Claude Code CLI reference documents the primitives needed for this design:
- `--input-format stream-json` and `--output-format stream-json` support a programmatic bidirectional streaming session.
- `--include-partial-messages` exposes incremental assistant text for a responsive chat UI.
- `--mcp-config` together with `--strict-mcp-config` provides an explicit per-invocation MCP server allowlist for Master Agent control-plane tools.
- `--resume <session-id>` resumes a CLI conversation; `--session-id <uuid>` can pin a session identifier.
- `--tools` restricts built-in tools separately from MCP tools, allowing the Master Agent to be configured without general-purpose shell or filesystem access.

References: [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference), [Claude Code MCP documentation](https://code.claude.com/docs/en/mcp).

These documented capabilities make the CLI-based design technically plausible and remove the need to invent a custom conversation protocol. They do not replace a real-host acceptance test. Phase 11.0 must verify the exact flags and event sequence on the operator's installed Claude Code 2.1.306, especially: multi-message stream input, partial text events, MCP tool invocation/result events, process cancellation, and session resume after orchestrator restart. The app's persisted conversation transcript remains authoritative even when CLI session resume is available. Session files are local CLI state and must be stored in a dedicated, permission-restricted Master Agent config directory; never point the Master Agent at the task Worker or user's unrestricted home configuration.

## Data model direction

Use additive migrations and nullable foreign keys to preserve existing installations and standalone tasks.

### Project

- `id` UUID primary key.
- `name`, unique `slug`, optional `description`.
- `workspace_path`: canonical default workspace root for this project.
- `status`: `active` or `archived`.
- `created_at`, `updated_at`, optional `archived_at`.

A project is an organizational boundary, not an OS security boundary. Workspace and sandbox validation remain enforced by the existing execution layer.

### Conversation

- `id` UUID primary key.
- Optional `project_id`; conversations outside a project are allowed.
- `title`, `status`, `created_at`, `updated_at`, optional `last_message_at`.

A conversation belongs to at most one project in the first release. Moving a conversation between projects must be an explicit user action and must not rewrite historical task associations.

### ConversationMessage

- Monotonic per-conversation sequence (unique with `conversation_id`), plus stable message ID.
- `role`: `user`, `assistant`, or `tool`.
- `content` and structured `metadata` (content blocks, tool-call correlation, error details).
- `created_at`; optional status for streamed/incomplete assistant messages.

Persist user messages before execution. Persist assistant output incrementally or as durable deltas so a browser refresh does not lose completed output. Never persist secrets or raw credential environment values in message metadata.

### AgentTurn and AgentTurnEvent

- AgentTurn: conversation, input message, status (`queued`, `running`, `succeeded`, `failed`, `cancelled`), adapter/session correlation, timestamps, and sanitized error summary.
- AgentTurnEvent: ordered, timestamped lifecycle, streaming, tool-call, dispatch, and failure events.
- One active turn per conversation in the first release; enforce this with a transactional claim/unique active-turn strategy.
- A turn is not a Task and must not be counted as Worker execution. Task/Workflow events remain independently durable and queryable.

The CLI documents `--resume <session-id>`, but the Worker’s previously observed `session_id` is not automatically proof that the corresponding transcript is available to a separate Master Agent process. Phase 11.0 must verify the session's persistence directory, ownership, and restart/resume behavior on the supported host. Conversation messages are the source of truth and must remain sufficient to reconstruct context if CLI resume is unavailable.

### Project associations

- Add nullable `project_id` to Workflow and Task.
- Workflow-created member tasks inherit the workflow's project.
- A standalone task may optionally belong to a project.
- Existing records remain valid with `project_id = NULL`.
- For the first release, task project association is immutable after dispatch; changing project ownership later requires an explicit migration/reassignment design.
- Validate that a workflow's tasks use the project's configured workspace policy; do not silently accept unrelated workspace paths.

### Dispatch/tool audit

Every Master Agent tool invocation that reads or changes control-plane state must be represented in AgentTurnEvent with a tool name, sanitized arguments/result summary, outcome, and related project/workflow/task IDs. Do not store prompts, tokens, or credentials in event payloads unless they are already intended user-visible content.

## API direction

Keep existing endpoints backward compatible. Add a versioned-by-resource set of endpoints (exact response schemas to be finalized during implementation):

### Projects
- `POST /projects`, `GET /projects`, `GET /projects/{project_id}`, `PATCH /projects/{project_id}`.
- `GET /projects/{project_id}/tasks`, `GET /projects/{project_id}/workflows`, `GET /projects/{project_id}/conversations`.
- Archive is preferred to deletion; deleting a project must not cascade-delete its tasks, events, or artifacts.

### Conversations
- `POST /conversations` (optional project association).
- `GET /conversations` (bounded list, optional project filter).
- `GET /conversations/{conversation_id}` and `GET /conversations/{conversation_id}/messages`.
- `POST /conversations/{conversation_id}/messages` creates a user message and durable AgentTurn.
- `POST /conversations/{conversation_id}/cancel` requests cancellation of the active Master Agent turn; this is separate from cancelling a Worker Task.
- A streaming endpoint (SSE preferred for the first browser implementation) streams persisted turn/message events with reconnect support using event IDs or sequence cursors.

### Agent and project tools
The Master Agent receives narrow tools for:
- listing/getting projects and current project context;
- creating a workflow or standalone task with an explicit project association;
- reading workflow/task status, task events, diagnostics, and artifact metadata;
- requesting cancellation of a task/workflow;
- requesting user approval for policy-gated operations.

The tool contract must validate every argument server-side. It must not expose raw SQL, arbitrary filesystem paths, shell execution, or unrestricted API proxying.

## Frontend direction

### AI Assistant page

- Chat is the default route and the dominant visual area.
- Left rail: conversations, new conversation, project filter/context.
- Main: transcript with clear user/assistant/tool activity, streaming assistant response, retry/error state, and composer.
- Optional collapsible right context panel: current project, proposed plan, linked workflow/tasks, approvals, and execution summary.
- Composer supports multiline input, stop/cancel, and attachment affordances only when upload semantics are implemented.
- Do not make task creation forms the primary home-page interaction.

### Projects pages

- Project list: name, status, workspace, recent activity, task counts by status.
- Project detail tabs: Overview, Tasks, Workflows, Agent activity, Files, Conversations.
- Clicking a task opens the existing task detail/timeline; clicking a workflow opens the existing workflow DAG/detail.
- Keep task/worker diagnostics available but secondary to project and conversation navigation.

### Responsive and accessibility

- Desktop: navigation rail + chat content + optional context panel.
- Narrow screens: navigation becomes a drawer; context panel becomes a sheet; composer remains reachable.
- Use semantic buttons/forms, keyboard-operable message composer and navigation, visible connection/stream state, and clear error/retry states.

## Security and reliability invariants

1. The browser never talks directly to Claude CLI or Worker subprocesses.
2. Master Agent and Worker processes have separate identities, lifecycle, configuration, and event streams.
3. Master Agent tool access is allowlisted and project-scoped; no generic shell or arbitrary filesystem tool is introduced by this phase.
4. Existing task permission modes and Bubblewrap fail-closed behavior are unchanged.
5. A project association never substitutes for workspace path validation or sandbox enforcement.
6. User messages and AgentTurns are durable before starting an Agent process.
7. Streaming reconnect reads persisted events; it is not an ephemeral-only socket.
8. Duplicate submit/reconnect requests must not create duplicate AgentTurns or duplicate workflows; use request idempotency keys and turn/tool-call correlation.
9. Cancellation of an AgentTurn is distinct from cancellation of tasks it already dispatched. The UI and audit trail must state which operation was requested.
10. With the current unauthenticated local API, the Master Agent is not a verified human identity boundary. Bind to localhost and retain the existing trusted-machine assumptions until authentication is designed.

## Compatibility and migration

- Do not rename or remove existing Task, Workflow, TaskEvent, Worker, or Artifact APIs.
- Existing dashboard routes remain available during the transition.
- Add project and conversation tables first; add nullable project foreign keys to tasks/workflows in a later additive migration.
- Existing standalone tasks and workflows are visible in the global Task Center with no project.
- Do not auto-create a project for old records.
- Preserve existing Worker lease, retry, repair, verification, execution trace, artifact, and sandbox behavior.

## Delivery plan

### Phase 11.0 — Architecture and runtime feasibility
- Verify Claude Code CLI stream-json input/output, partial-message events, explicit MCP tool bridge, process cancellation, and `--resume` behavior on the supported CLI version (currently 2.1.306).
- Verify a dedicated Master Agent config/session directory and confirm no user-global or Worker MCP/settings leakage.
- Define Master Agent/Worker trust boundaries and tool schemas.
- Finalize database relationships, event contracts, SSE replay semantics, and migration order.
- Deliver architecture review and acceptance matrix before changing execution behavior.

### Phase 11.1 — Project foundation
- Add Project model, additive migrations, CRUD/read APIs, project association on Workflow/Task, and project summary queries.
- Keep old task/workflow creation API behavior unchanged.
- Add API tests for migration compatibility, project scoping, and summary counts.

### Phase 11.2 — Durable conversations
- Add Conversation, ConversationMessage, AgentTurn, and AgentTurnEvent persistence.
- Add conversation APIs, one-active-turn concurrency, idempotent message submission, cancellation state, and persisted streaming/replay.
- Initially use a deterministic mock Master Agent adapter for API and browser tests.

### Phase 11.3 — Master Agent adapter and control-plane tools
- Implement the Claude Code CLI adapter only after Phase 11.0 confirms the supported invocation and tool bridge.
- Add narrowly scoped project/task/workflow read/write tools with server-side validation and complete audit events.
- Prove that Worker MCP isolation and Bubblewrap policies remain unchanged.

### Phase 11.4 — Chat-first frontend
- Replace the task-console landing page with AI Assistant chat.
- Add conversation navigation, streaming transcript, composer, cancellation, and optional project context panel.
- Preserve existing task/workflow detail components under Project and Task Center navigation.

### Phase 11.5 — Project management UI
- Add project directory, project overview, project task/workflow tabs, activity, files, and conversations.
- Add cross-project Task Center and Execution Monitor navigation.
- Add end-to-end tests for project-to-chat-to-workflow-to-task-result journeys.

## Acceptance criteria

### Product behavior
- A new user can start from the chat page without manually filling a Task or Workflow form.
- A conversation can be created globally or within a selected project.
- The Master Agent can inspect current project context, ask clarifying questions, propose a plan, and create a project-linked workflow through audited tools.
- The user can see the created workflow and its member tasks in the project page while execution is running.
- The Master Agent can read persisted task results and provide a final answer in the same conversation.
- A user can open a project and inspect task state, dependencies, worker activity, events, and artifacts without searching chat history.
- Existing standalone tasks/workflows remain visible and operable.

### Reliability and safety
- User messages and turns survive API/Worker restart.
- Stream reconnect replays events without duplicating messages or actions.
- A cancelled AgentTurn cannot dispatch additional tasks after cancellation is acknowledged.
- Cancelling a turn does not silently cancel already-running Tasks; task cancellation is explicit and separately audited.
- Master Agent tool calls cannot access arbitrary host shell/filesystem or bypass project/workspace policy.
- Existing API/unit tests and three-round browser E2E suite remain green.
- A real-host test verifies a complete chat → Master Agent dispatch → Worker execution → result synthesis path before the feature is declared ready.

## Explicit non-goals for the first release

- Multi-user accounts, remote internet exposure, and organization-level RBAC.
- Arbitrary autonomous background work without a user-started turn.
- General-purpose shell access for the Master Agent.
- Automatic project discovery from arbitrary host directories.
- Pause/resume of Worker CLI sessions.
- Replacing SQLite, the existing scheduler, or the existing Worker execution engine.
