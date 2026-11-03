# Phase 6.5 — Execution Insights & Cost Tracking

## Scope

Phase 6.5 adds a read-only aggregate endpoint and a compact Dashboard view. It reuses Task, Workflow, TaskEvent, and the current Worker registry; it adds no database tables, migrations, dependencies, or background collectors.

## Metric definitions

- Queue wait: tasks.created_at → tasks.started_at. A task cancelled before it starts uses finished_at as its observed queue lifetime. Only records with both endpoints contribute to the average.
- Execution duration: tasks.started_at → tasks.finished_at. Only completed execution intervals contribute; running tasks are not mixed into historical averages.
- End-to-end task duration: tasks.created_at → tasks.finished_at.
- Task success rate: succeeded tasks divided by terminal tasks (succeeded, failed, cancelled, blocked).
- Workflow wall-clock duration: workflows.started_at → workflows.finished_at.
- Workflow end-to-end duration: workflows.created_at → workflows.finished_at.
- Cumulative workflow task execution: sum of member task execution intervals. Parallel DAG tasks overlap, so this is worker execution time, not workflow wall-clock time.
- Throughput: terminal tasks created within the selected window divided by the selected number of days.

The period selector filters by task/workflow creation time. Averages include only valid timestamp pairs; incomplete records are excluded rather than assigned zero.

## Usage and cost provenance

Claude Code's persisted claude.result events are the only source used for usage and cost:
- total_cost_usd is summed only when a result event contains a non-negative numeric value.
- Token totals include only numeric input_tokens, output_tokens, cache_read_input_tokens, and cache_creation_input_tokens fields found under the event's usage object.
- Missing fields are not inferred. If no result event reports a value, cost is null / “Not collected”; no dollar cost is calculated from token counts or guessed model prices.
- Coverage counts are exposed so a partial total is not mistaken for complete coverage. Retries may produce multiple result events and each reported result is counted because each represents an actual agent invocation.

## Worker utilization limitation

The Worker table stores only the latest heartbeat/status, not a historical heartbeat series. The endpoint therefore reports a clearly labelled current snapshot (busy/idle/offline/lease-expired and current slots) and sets historical_utilization_available: false. It does not present snapshot ratios as historical utilization.

## API

GET /insights?period_days=30

period_days accepts 1–365. The response includes task and workflow aggregates, reported usage/cost coverage, current worker snapshot, and explicit limitations. The endpoint is read-only.

## Dashboard

Execution Insights includes a 7/30/90/365-day selector, task and workflow success rates, average queue/execution durations, throughput, reported Claude cost/token coverage, and a worker snapshot. Detailed task diagnostics and replay remain available in task detail.
