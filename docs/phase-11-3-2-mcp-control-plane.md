# Phase 11.3.2 — Master Agent MCP Control Plane

## Goal

Give the durable Master Agent a small, auditable MCP surface for inspecting the active project's state and proposing queued work. The MCP server is scoped to the conversation that launched the turn; model-supplied project identifiers are never treated as authority.

## Tool contract

The first implementation exposes only these tools:

- `get_project_overview`: active project identity, workspace path, and aggregate task/workflow counts.
- `list_tasks`: tasks belonging to the active project, with bounded pagination and status filtering.
- `get_task`: one task, only if it belongs to the active project.
- `list_workflows`: workflows belonging to the active project.
- `get_workflow`: one workflow and its task/dependency summary, only if it belongs to the active project.
- `create_task`: create a queued task in the active project.
- `create_workflow`: create a queued workflow and its dependency graph in the active project.

A conversation without a project receives no project/task/workflow tools that can access arbitrary projects. It may use a read-only `list_projects` discovery tool only if explicitly enabled in a later product decision; this slice does not expose it.

## Security invariants

1. The server receives the conversation ID and project ID from the trusted Master Agent launcher, not from tool arguments.
2. Every query and write includes the server-bound project ID. A requested task/workflow ID is resolved and then checked against that project before returning data.
3. Tool schemas do not accept `project_id` or `workspace_path`.
4. Task creation derives `workspace_path` from the bound project's canonical workspace and rejects archived projects.
5. MCP-created tasks and workflow tasks always use `permission_mode=plan`, `auto_repair=false`, and bounded retry/verification settings. The model cannot grant itself configured write permissions.
6. Creation only inserts queued records and audit events. It never claims work, starts a Worker, or executes a task.
7. Workflow creation validates unique keys, dependency references, and acyclicity using the same validation rules as the API.
8. The MCP config names exactly the approved server. The Claude command removes the blanket `mcp__*` deny rule and instead denies every MCP tool except the explicit allowlist; built-in shell and file-edit tools remain denied.
9. No MCP tool can cancel, retry, edit, archive, or delete existing work in this slice.

## Current implementation

The server lives in `ai_work_os.mcp_control_plane` and implements newline-delimited JSON-RPC 2.0 for MCP initialize, ping, tools/list, and tools/call. It is started by Claude Code from an isolated per-turn MCP config named `awos`.

The Claude invocation allows only `mcp__awos__*` MCP tools and continues to deny Bash, Edit, Write, and NotebookEdit. Projectless conversations receive an empty MCP server configuration. The Master Agent system prompt now directs it to inspect project state and queue work through these tools.

Task creation stores a plan-mode task using the project's canonical workspace and writes a `task.queued` audit event. Workflow creation validates task keys, dependency references, duplicate edges, and DAG acyclicity before inserting the workflow, tasks, dependencies, and queued events in one transaction. Neither operation starts a Worker.

Read operations are scoped by the trusted project ID injected into the server environment. Cross-project task/workflow IDs return the same not-found response as missing IDs. Archived projects remain inspectable but cannot receive new work.

## Runtime and sandbox requirements

The MCP server is a dedicated stdio child process launched by Claude Code. It must use the same database configuration as AI Work OS and must not inherit the user's real Claude configuration.

Current limitation: project-scoped MCP turns are deliberately rejected when Bubblewrap is enabled. The MCP server needs a narrowly scoped read/write database mount plus the Python package/runtime; this mount contract is not yet implemented. The Master Agent fails closed rather than allowing the MCP child to inherit host database access outside the sandbox. Project-scoped MCP is currently available only with sandbox mode off. Bubblewrap support is a required follow-up before enabling both features together.

## Auditability

Each successful creation writes the same task/workflow records and queued events as the HTTP API. The Master Agent event stream records tool calls/results, while the task/workflow event stream remains the durable execution audit. Tool output is bounded and omits credentials and absolute paths unrelated to the active project.

## Acceptance plan

- Verify MCP initialize/list-tools/call protocol with the exact supported Claude Code CLI.
- Verify every tool is bound to the launching conversation's project.
- Attempt cross-project task/workflow ID access and confirm no data is returned.
- Attempt to pass project IDs, workspace paths, configured permission mode, shell commands, or arbitrary MCP config through tool arguments and confirm rejection/absence from schemas.
- Verify create_task creates exactly one queued plan-mode task and a queued event; no Worker starts as a side effect.
- Verify create_workflow creates an atomic workflow/task/dependency graph and rejects cycles, duplicate keys, missing dependencies, and cross-project scope.
- Verify archived and projectless conversations cannot create project work.
- Verify cancellation, lease fencing, session resume, and stream persistence still work with MCP enabled.
- Verify project-scoped turns fail closed under Bubblewrap until the dedicated runtime/database mounts are implemented.


## MCP permission mode and real-CLI regression

The Master Agent CLI uses Claude Code's `default` permission mode, not `plan`.
Claude Code plan mode rejects MCP tool execution even when the server is connected
and the tool name matches `--allowedTools`. This worker is non-interactive and sets
`--permission-prompts none`, so only the explicit `mcp__awos__*` allowlist can run.
The built-in tool list remains restricted to `Read,Glob,Grep`; Bash, Edit, Write,
and NotebookEdit remain explicitly denied. The MCP server itself exposes only the
project-scoped read and queued-work tools described above. Task records created by
MCP remain `permission_mode=plan`, and creation never starts a Worker.

Run the opt-in real Claude Code regression against an isolated temporary database:

```bash
AI_WORK_OS_RUN_CLAUDE_MCP_ACCEPTANCE=1 \
  .venv/bin/pytest -q tests/test_master_agent_cli_acceptance.py
```

The test requires a working Claude Code CLI and inherited provider credentials.
It exercises `get_project_overview`, `list_tasks`, `create_task`, and
`create_workflow` through the actual MCP stdio process, then verifies the
persisted queued task/workflow DAG. It does not start the AI Work OS task Worker.


## Conversation-scoped Claude CLI session continuity

The Claude CLI session identifier is stored on `conversations.cli_session_id`,
while `agent_turns.cli_session_id` remains as a per-turn audit snapshot. Each
new turn resumes the conversation's latest session. The additive SQLite
compatibility migration backfills the conversation field from the newest
historical turn session ID. Turn claiming is serialized per conversation so
two Master Agent workers cannot concurrently resume the same CLI session.

The real CLI acceptance requires `AI_WORK_OS_CLAUDE_MODEL` to name a model
available through the configured gateway. It now performs a follow-up CLI
invocation using the first turn's session ID and verifies the same session is
returned. If the model variable is absent, pytest skips the opt-in test; the
standalone script prints an actionable configuration error instead of
misreporting a gateway 503 as an MCP permission failure.
