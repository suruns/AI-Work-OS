# Phase 6.3: Workflow execution trace and diagnostics

## What changed

- Added `GET /workflows/{workflow_id}/events` to merge member-task events into one chronological workflow stream.
- Each event includes its task ID and title so callers can navigate from a workflow event to the responsible task.
- Thinking-token updates are excluded by default to keep the workflow timeline readable; pass `include_thinking_tokens=true` when raw token events are explicitly needed.
- The endpoint accepts a bounded `limit` (1–1000, default 200) and returns 404 for an unknown workflow.
- The Dashboard workflow detail now shows member steps, a merged execution timeline, and a compact attention summary for failed or blocked tasks.
- Selecting an event or using **Inspect task** opens that task in the existing task detail panel.

## Data and compatibility

This phase reuses the existing `TaskEvent`, `Task`, and `Workflow` records. It adds no database tables, columns, migration, background process, or external dependency. Existing per-task event APIs and worker lifecycle/recovery behavior are unchanged.

The timeline is ordered by event timestamp and event ID. It is an observability view, not a new execution or retry mechanism.

## API example

```http
GET /workflows/{workflow_id}/events?limit=200
```

Example event shape:

```json
{
  "id": 42,
  "task_id": "task-uuid",
  "task_title": "Run verification",
  "occurred_at": "2026-11-03T10:00:00",
  "event_type": "executor.finished",
  "payload": {
    "status": "failed",
    "error_summary": "verification command failed"
  }
}
```

## Local validation

```bash
python -m pip install -e ".[dev,e2e]"
pytest -q -m "not e2e"
python -m playwright install chromium
for attempt in 1 2 3; do
  pytest -q tests/e2e -m e2e || exit 1
done
git diff --check origin/main...HEAD
```

The browser suite uses an isolated SQLite database and a local API process. It does not start a Worker or Claude Code process and does not require external credentials.
