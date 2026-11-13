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

## Runtime and sandbox requirements

The MCP server is a dedicated stdio child process launched by Claude Code. It must use the same database configuration as AI Work OS and must not inherit the user's real Claude configuration.

When Bubblewrap is enabled, the MCP child must remain inside the same fail-closed sandbox. The sandbox must expose only the minimum application code/runtime and database access needed by this server; it must not mount the user's home, unrelated project workspaces, or general application secrets. If that constrained mount contract cannot be satisfied, startup must fail closed rather than silently run the MCP server on the host.

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
- Verify Bubblewrap mode remains fail-closed and the MCP child has no access beyond the explicit runtime/database mounts.
