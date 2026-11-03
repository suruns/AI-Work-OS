# Phase 6.4: Execution replay and failure analysis

## Scope

Phase 6.4 adds a read-only task diagnostics endpoint and a Dashboard view that explains terminal task outcomes using persisted task fields and structured events.

- `GET /tasks/{task_id}/diagnostics` returns task context, evidence facts, a failure classification, evidence-linked diagnostic hypotheses, and a chronological replay.
- Replay entries are the original persisted `TaskEvent` records, ordered by `occurred_at` and then event ID. Each entry is explicitly marked as a recorded event; the API does not fabricate missing lifecycle events.
- Failure categories include verification failure, execution failure, timeout, cancellation, worker/lease loss, and dependency blocking. A successful or otherwise unclassified task returns a null classification and `unknown` confidence.
- Dashboard Task detail displays the failure analysis and execution replay. Workflow diagnostics continue to link attention tasks into Task detail.

## Evidence and safety

- Facts identify their source field or event ID.
- Hypotheses are not asserted as proven root causes. Suggested next steps are advisory only.
- The endpoint does not retry tasks, mutate task/workflow state, execute commands, or access the workspace.
- Raw stdout/stderr contents are not returned. Existing task prompt and structured task metadata are included as context, consistent with the existing Task detail API.
- The response documents replay and diagnosis limitations, including the possibility of missing events.

## Compatibility

This phase reuses `Task`, `TaskEvent`, and the existing Dashboard. It adds no database tables, columns, migrations, background workers, or dependencies.

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

The browser suite uses an isolated SQLite database and a local API process. It does not start Claude Code or a Worker and does not require external credentials.
