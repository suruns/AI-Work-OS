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
- `POST /workflows` — atomically create a workflow and its task DAG
- `GET /workflows` — list workflows with task-status counts
- `GET /workflows/{workflow_id}` — workflow, member tasks, dependency edges, and blocked reasons
- `POST /workflows/{workflow_id}/cancel` — cancel queued members and request cancellation for running members

Task creation accepts `priority` (0–100, default 0) and `max_attempts` (1–3, default 1). Phase 3 adds optional `acceptance_criteria` text and up to five `verification_commands` (one command per string). After Claude exits successfully, the worker runs each configured command in the workspace with argument parsing but without a shell, stops on the first failure, and applies a 120-second timeout per command. A failed verification makes the task status `failed` even if Claude exited with code 0; `verification_status` distinguishes `not_configured`, `pending`, `passed`, `failed`, and `not_run`. Results are recorded in `verification.finished` and `verification.json`. Verification commands are still executable programs and can have side effects; use trusted workspaces and only configure commands you trust. Higher-priority tasks are claimed first; tasks at equal priority remain FIFO by creation time. Retries are opt-in (`max_attempts` greater than 1), use exponential backoff controlled by `AI_WORK_OS_RETRY_BASE_DELAY_SECONDS` (default 2 seconds), and are not automatically retried after a timeout or cancellation. The task record exposes `attempt_count`. Each attempt keeps its own `stdout.attempt-NNN.jsonl` and `stderr.attempt-NNN.log` artifacts; `stdout.jsonl` and `stderr.log` remain aliases for the latest attempt for compatibility. The worker runs up to `AI_WORK_OS_MAX_CONCURRENT_TASKS` tasks concurrently (default 1). Each worker process has a unique identity and claims tasks with a time-limited lease. `AI_WORK_OS_WORKER_LEASE_SECONDS` defaults to 60 seconds and `AI_WORK_OS_WORKER_HEARTBEAT_SECONDS` defaults to 10 seconds. A worker renews its lease while executing; completion is fenced by worker identity so a stale worker cannot overwrite a recovered task. Startup recovery only fails expired leases (exit code 125, no automatic retry). Legacy `running` rows without a lease receive one lease-length grace period based on `started_at` before recovery. If upgrading from an older release, stop old worker processes before starting the new version: the lease prevents a new worker from reclaiming a live task, but it cannot forcibly terminate an old process running code without lease support. Inspect workspace changes and surviving agent processes before manually retrying.

Task responses expose `queue_wait_ms` and `execution_ms` separately. The worker records an `executor.claimed` event with queue-wait timing, so delayed dispatch can be distinguished from slow agent execution. The dashboard requests a compact timeline that folds repeated `thinking_tokens` updates into one summary, reducing payload size while preserving raw JSONL output. Use `/tasks/{task_id}/events?include_thinking_tokens=true` to retrieve every raw event for debugging.

Cancelling a queued task immediately marks it cancelled and records a terminal event. Running-task cancellation is observed by the worker within its polling interval; on POSIX systems the worker starts Claude Code in a separate process session and terminates its process group on cancellation or timeout, escalating from SIGTERM to SIGKILL if needed. Worker coroutine interruption also attempts to stop the subprocess tree and records a distinct event. These lifecycle paths are covered by regression tests.

For Git workspaces, the worker snapshots tracked diffs and non-ignored untracked files before and after execution and writes `storage/tasks/<task-id>/workspace_changes.json`. The report separates paths that were already changed before the task from net changes observed during it, and the event timeline includes a `workspace.changes_reported` summary. The report is available through `GET /tasks/{task_id}/workspace-changes` and is shown directly in the dashboard task detail. Click a listed file to inspect its text diff through `GET /tasks/{task_id}/workspace-diff?path=...`; the endpoint only serves paths listed in that task's report, rejects traversal and symlink paths, and caps displayed diffs. The diff compares the current workspace with `HEAD`, so pre-existing edits can appear in it and are labelled separately. It stores file paths and hashes, not source contents. When Claude Code emits its final `result` event, the dashboard also shows the final response separately from the raw execution timeline, along with duration, cost, and usage fields when provided by the CLI. It is a before/after snapshot rather than a full filesystem audit: changes made and reverted during execution may not be visible, ignored files are excluded, and non-Git workspaces are reported as unavailable. Review the report alongside task logs before attributing changes to the agent.


The task detail page includes an **Artifacts** section with links to the execution JSONL, stderr log, workspace-change report, and current workspace files observed as changed during that task. Downloads are restricted to a fixed set of task-owned artifacts or file paths explicitly listed in the task's change report; path traversal and symlink downloads are rejected. Workspace file downloads are current files, not a historical copy, so review the diff and logs before using them. Treat all downloaded logs and workspace files as sensitive data.
The initial worker claims queued tasks from SQLite and invokes Claude Code with --print --output-format stream-json --verbose. Unknown event types are retained as raw JSON so the parser can evolve with CLI versions.


Phase 4 adds opt-in bounded repair after verification failure. Set `auto_repair: true` and `max_repair_attempts` (1–2) when creating a task; both default to disabled/0. Repair attempts are tracked separately from normal task attempts and recorded as `repair.started` / `repair.finished` events, with per-repair stdout/stderr artifacts. The repair prompt includes bounded failed verification output and asks the agent to make the smallest correction without weakening checks. Verification commands are rerun after each repair, and the task succeeds only when they pass. Repair uses the same Claude permission mode as the original task and is bounded to at most two repair attempts; it is not run for timeout/cancellation or when disabled. This is not a security sandbox: repair can modify the trusted workspace and verification commands can have side effects. Review diffs and logs before accepting changes.


## Workflow orchestration

Phase 5 adds workflows containing up to 100 tasks. Each task has a stable client-side `key` and an optional `depends_on` list of other task keys. Workflow creation validates references and rejects duplicate keys, self-dependencies, and cycles before inserting the workflow, tasks, and dependency edges in one database transaction.

Example:
~~~bash
curl -X POST http://127.0.0.1:8000/workflows \
  -H 'content-type: application/json' \
  -d '{
    "title": "Implement and verify a feature",
    "tasks": [
      {"key":"inspect","title":"Inspect code","prompt":"Inspect the existing implementation and report a plan.","workspace_path":"/absolute/path/to/project"},
      {"key":"implement","title":"Implement feature","prompt":"Implement the agreed feature.","workspace_path":"/absolute/path/to/project","depends_on":["inspect"]},
      {"key":"verify","title":"Verify feature","prompt":"Review the implementation and run the project checks.","workspace_path":"/absolute/path/to/project","depends_on":["implement"]}
    ]
  }'
~~~

A workflow task is claimable only after every direct prerequisite has succeeded and its own retry backoff has expired. A prerequisite waiting for retry remains non-terminal and does not release dependents. When a prerequisite fails, is cancelled, or is blocked, queued descendants become `blocked` with a recorded reason. Workflow status is aggregated from member task states. Dependencies are immutable after creation; create a new workflow to change the DAG. Existing `POST /tasks` remains available for standalone tasks.

The dashboard supports creating workflows with prerequisite selection, inspecting member task states and blocked reasons, and cancelling a workflow. Workflow tasks continue to use the existing Claude permission mode, verification, retry, and opt-in repair behavior. Workflow orchestration is not an OS sandbox; use trusted workspaces and trusted verification commands.
