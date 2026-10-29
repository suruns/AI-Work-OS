# AI Work OS

A control plane around existing coding agents. The first vertical slice targets Claude Code CLI and focuses on durable task records, execution events, logs, and a small HTTP API.

## Status
Local MVP for a single trusted developer machine. The API and worker are separate processes; SQLite is the task store. Authentication, multi-user isolation, and production deployment hardening are out of scope.

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

- `GET /` — task console
- `GET /health`
- `GET /stats`
- `POST /tasks`
- `GET /tasks`
- `GET /tasks/{task_id}`
- `GET /tasks/{task_id}/events`
- `GET /tasks/{task_id}/workspace-changes`
- `GET /tasks/{task_id}/workspace-diff?path=relative/file/path`
- `GET /tasks/{task_id}/artifacts`
- `GET /tasks/{task_id}/artifacts/{name}`
- `GET /tasks/{task_id}/workspace-file?path=relative/file/path`
- `POST /tasks/{task_id}/cancel`

Task creation accepts `priority` (0–100, default 0) and `max_attempts` (1–3, default 1). Higher-priority tasks are claimed first; tasks at equal priority remain FIFO by creation time. Retries are opt-in (`max_attempts` greater than 1), use exponential backoff controlled by `AI_WORK_OS_RETRY_BASE_DELAY_SECONDS` (default 2 seconds), and are not automatically retried after a timeout or cancellation. The task record exposes `attempt_count`; task logs currently represent the most recent attempt. The worker runs up to `AI_WORK_OS_MAX_CONCURRENT_TASKS` tasks concurrently (default 1). Concurrency is per worker process, so run only one worker unless you coordinate multiple workers externally.

Task responses expose `queue_wait_ms` and `execution_ms` separately. The worker records an `executor.claimed` event with queue-wait timing, so delayed dispatch can be distinguished from slow agent execution. The dashboard requests a compact timeline that folds repeated `thinking_tokens` updates into one summary, reducing payload size while preserving raw JSONL output. Use `/tasks/{task_id}/events?include_thinking_tokens=true` to retrieve every raw event for debugging.

Cancelling a queued task immediately marks it cancelled and records a terminal event. Running-task cancellation is observed by the worker within its polling interval; on POSIX systems the worker starts Claude Code in a separate process session and terminates its process group on cancellation or timeout, escalating from SIGTERM to SIGKILL if needed. Worker coroutine interruption also attempts to stop the subprocess tree and records a distinct event. These lifecycle paths are covered by regression tests.

For Git workspaces, the worker snapshots tracked diffs and non-ignored untracked files before and after execution and writes `storage/tasks/<task-id>/workspace_changes.json`. The report separates paths that were already changed before the task from net changes observed during it, and the event timeline includes a `workspace.changes_reported` summary. The report is available through `GET /tasks/{task_id}/workspace-changes` and is shown directly in the dashboard task detail. Click a listed file to inspect its text diff through `GET /tasks/{task_id}/workspace-diff?path=...`; the endpoint only serves paths listed in that task's report, rejects traversal and symlink paths, and caps displayed diffs. The diff compares the current workspace with `HEAD`, so pre-existing edits can appear in it and are labelled separately. It stores file paths and hashes, not source contents. When Claude Code emits its final `result` event, the dashboard also shows the final response separately from the raw execution timeline, along with duration, cost, and usage fields when provided by the CLI. It is a before/after snapshot rather than a full filesystem audit: changes made and reverted during execution may not be visible, ignored files are excluded, and non-Git workspaces are reported as unavailable. Review the report alongside task logs before attributing changes to the agent.


The task detail page includes an **Artifacts** section with links to the execution JSONL, stderr log, workspace-change report, and current workspace files observed as changed during that task. Downloads are restricted to a fixed set of task-owned artifacts or file paths explicitly listed in the task's change report; path traversal and symlink downloads are rejected. Workspace file downloads are current files, not a historical copy, so review the diff and logs before using them. Treat all downloaded logs and workspace files as sensitive data.
The initial worker claims queued tasks from SQLite and invokes Claude Code with --print --output-format stream-json --verbose. Unknown event types are retained as raw JSON so the parser can evolve with CLI versions.
