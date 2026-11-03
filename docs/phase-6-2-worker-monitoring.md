# Phase 6.2: Worker registry and monitoring

## What changed

- A persistent Worker registry records worker ID, hostname, PID, process start time, last heartbeat, configured concurrency, and active task count.
- Worker liveness is refreshed while polling the queue and while renewing an active task lease.
- Graceful shutdown marks the worker offline. A crashed process is detected when its heartbeat becomes older than the configured threshold.
- The API derives the displayed state from heartbeat age and task leases:
  - \`idle\`: recent heartbeat, no active task.
  - \`busy\`: recent heartbeat, one or more active tasks.
  - \`offline\`: heartbeat is older than the offline threshold.
  - \`lease_expired\`: at least one assigned running task has an expired lease.
- The Dashboard shows worker host, PID, active slots, last heartbeat, and active task names.

The task's \`worker_id\` and \`lease_expires_at\` remain authoritative for ownership and recovery. Worker registry data is observability only; it does not claim tasks, renew task leases, or automatically retry work.

## API

\`GET /workers\` returns:

- \`total\`
- \`by_status\` counts
- \`workers[]\`, including worker identity, heartbeat age, derived status, concurrency, and active task lease diagnostics.

The default offline threshold is three worker heartbeat intervals, with a minimum of 15 seconds. Override it with:

    AI_WORK_OS_WORKER_OFFLINE_AFTER_SECONDS=45

The existing \`AI_WORK_OS_WORKER_HEARTBEAT_SECONDS\` setting continues to control active task lease renewal cadence and the default worker liveness threshold.

## Local validation

    python -m pip install -e ".[dev,e2e]"
    python -m pytest -q -m "not e2e"
    python -m playwright install chromium
    for attempt in 1 2 3; do
      pytest -q tests/e2e -m e2e || exit 1
    done
    git diff --check origin/main...HEAD

To see a live worker in the Dashboard, start the API and a worker against the same database and storage configuration. In another terminal, from the repository root:

    python -m ai_work_os.worker

Stop the worker with Ctrl+C to observe its graceful offline transition. If the process is killed unexpectedly, it will appear offline after the heartbeat timeout. If it owned a task whose lease has expired, the API reports \`lease_expired\` until the existing orphan recovery process handles it.

No real Claude process is needed for the API and browser tests. The E2E test inserts isolated registry rows directly into its per-test SQLite database.
