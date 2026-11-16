# Phase 11.3.3 — Bubblewrap Project MCP Broker

## Goal

Allow project-scoped Master Agent MCP tools while Claude Code runs inside Bubblewrap, without exposing the host database, application storage, or unrestricted host runtime to the sandbox.

## Trust boundary

The sandboxed MCP stdio process is a protocol adapter only. In Bubblewrap mode it receives `AI_WORK_OS_MCP_BROKER_SOCKET=/run/awos-mcp.sock` and does not initialize SQLAlchemy, load the host database URL, or receive a project identifier. The socket is a per-turn Unix domain socket created by the trusted Master Agent process, mode `0600`, and bind-mounted at one fixed path.

The host-side broker captures the project ID from the trusted Conversation/Project lookup. Requests contain only a tool name and tool arguments; the client cannot supply a project ID, workspace path, database URL, or filesystem path. The broker calls the existing project-scoped MCP implementation. Cross-project not-found behavior, archived-project write denial, plan-only task creation, and queue-only semantics remain authoritative.

The sandbox receives only read-only mounts for the Python runtime/package roots needed by the MCP protocol adapter, the isolated MCP config, and the single broker socket. It does not receive a database file, SQLite WAL/SHM files, application storage directory, or host runtime write mount. Workspace access remains read-only for the Master Agent. Network policy remains controlled by the existing Bubblewrap setting.

## Lifecycle and failure behavior

The broker is created before Claude starts and closed after the turn exits, is cancelled, loses its lease, or fails. The socket is removed during cleanup. The client uses bounded newline-delimited JSON, bounded response sizes, and a request timeout. Missing, malformed, non-socket, symlinked, out-of-scope, or overlong socket paths fail closed. There is no fallback to host database access or unsandboxed execution.

Projectless conversations continue to receive no MCP server. Sandbox-off conversations retain the existing direct stdio MCP server path. This phase does not grant Bash, Edit, Write, NotebookEdit, arbitrary MCP servers, or task execution permissions.

## Acceptance plan

- Inspect the generated Bubblewrap command and prove only the fixed Unix socket is exposed for database operations.
- Verify the sandboxed MCP adapter does not import or initialize the host DB module in broker mode.
- Verify broker requests use the trusted project binding, including cross-project IDs and archived-project behavior.
- Run real Claude CLI MCP acceptance with Bubblewrap enabled; verify read tools, task/workflow creation, and no Worker claim.
- Verify the broker socket is removed after success, cancellation, timeout, lease loss, and startup failure.
- Verify sandbox-off MCP acceptance and all existing Worker/Dashboard tests remain unchanged.


## Real Bubblewrap + Claude CLI acceptance

Run this on a Linux host where Bubblewrap user namespaces are available, with a gateway-supported model and valid provider credentials already present in the environment. The sandbox must be able to reach the model gateway; use host networking for this acceptance only:

```bash
AI_WORK_OS_RUN_BUBBLEWRAP_MCP_ACCEPTANCE=1 \
AI_WORK_OS_SANDBOX_MODE=bubblewrap \
AI_WORK_OS_SANDBOX_NETWORK=host \
AI_WORK_OS_CLAUDE_MODEL=deepseek-flash \
.venv/bin/pytest -q tests/test_master_agent_bubblewrap_acceptance.py
```

The acceptance drives the actual Master Agent turn executor (not a hand-built CLI command), performs four project MCP calls, creates a queued DAG, runs a follow-up turn with the persisted Claude session, checks the broker mount metadata, verifies that the host database path is absent from the sandbox invocation record, confirms no task Worker started, and checks that both per-turn sockets were removed.
