# AI Work OS

A control plane around existing coding agents. The first vertical slice targets Claude Code CLI and focuses on durable task records, execution events, logs, and a small HTTP API.

## Status
Early MVP scaffold. The API and worker are separate processes; SQLite is intended for local single-machine development. This is not yet a production multi-user deployment.

## Requirements
- Python 3.11+
- Claude Code CLI installed and authenticated (claude --version, claude auth status)
- A trusted project directory to use as a workspace

## Run locally
~~~bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
uvicorn ai_work_os.api:app --reload
~~~

In another terminal, start the worker:
~~~bash
source .venv/bin/activate
python -m ai_work_os.worker
~~~

Create a task:
~~~bash
curl -X POST http://127.0.0.1:8000/tasks -H 'content-type: application/json' -d '{"title":"Inspect project","prompt":"Only list files and summarize the project. Do not modify files.","workspace_path":"/absolute/path/to/a/trusted/project"}'
~~~

Open http://127.0.0.1:8000/ for the task console and /docs for the API.

## Safety
- Never run this service against untrusted workspaces.
- The worker does not add --dangerously-skip-permissions or bypassPermissions.
- CLI output is stored under storage/tasks/<task-id>/; it may contain source code or secrets. Protect this directory.
- Bind the API to localhost unless authentication and network controls have been configured.
- New tasks default to Claude Code `plan` permission mode (read-only). Choose "Use local Claude Code settings" only when the task is expected to edit files; that mode deliberately leaves permission behavior to the CLI's local configuration.
- Plan mode is a Claude Code permission control, not an OS-level sandbox. Continue to use trusted workspaces and review agent activity.

## Initial API
- GET / (task console)\n- GET /stats\n- GET /health
- POST /tasks
- GET /tasks
- GET /tasks/{task_id}
- GET /tasks/{task_id}/events
- POST /tasks/{task_id}/cancel

Task responses expose `queue_wait_ms` and `execution_ms` separately. The worker records an `executor.claimed` event with queue-wait timing, so delayed dispatch can be distinguished from slow agent execution. The dashboard requests a compact timeline that folds repeated `thinking_tokens` updates into one summary, reducing payload size while preserving raw JSONL output. Use `/tasks/{task_id}/events?include_thinking_tokens=true` to retrieve every raw event for debugging.

Cancelling a queued task immediately marks it cancelled and records a terminal event. Running-task cancellation is observed by the worker within its polling interval; on POSIX systems the worker starts Claude Code in a separate process session and terminates its process group on cancellation or timeout, escalating from SIGTERM to SIGKILL if needed. Worker coroutine interruption also attempts to stop the subprocess tree and records a distinct event. These lifecycle paths are covered by regression tests.

The initial worker claims queued tasks from SQLite and invokes Claude Code with --print --output-format stream-json --verbose. Unknown event types are retained as raw JSON so the parser can evolve with CLI versions.
