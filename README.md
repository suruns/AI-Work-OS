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

Browse /docs for the API.

## Safety
- Never run this service against untrusted workspaces.
- The worker does not add --dangerously-skip-permissions or bypassPermissions.
- CLI output is stored under storage/tasks/<task-id>/; it may contain source code or secrets. Protect this directory.
- Bind the API to localhost unless authentication and network controls have been configured.
- Review changes produced by an agent before accepting them.

## Initial API
- GET /health
- POST /tasks
- GET /tasks
- GET /tasks/{task_id}
- GET /tasks/{task_id}/events
- POST /tasks/{task_id}/cancel

The initial worker claims queued tasks from SQLite and invokes Claude Code with --print --output-format stream-json --verbose. Unknown event types are retained as raw JSON so the parser can evolve with CLI versions.
