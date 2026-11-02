# Phase 5: Workflow orchestration

## Goal

Add durable workflow records and dependency-aware task scheduling while preserving the existing task execution lifecycle. A workflow is a group of tasks with a directed acyclic dependency graph (DAG).

## First-release contract

### Data model

- `Workflow`: UUID, title, status, created/started/finished timestamps.
- `WorkflowTaskDependency`: workflow ID, upstream task ID, downstream task ID.
- Tasks remain independently addressable through the existing task API.
- A task may belong to at most one workflow in this release.
- Dependencies are immutable after workflow submission in the first release. This avoids races with tasks already claimed by workers.

### Statuses

- Workflow: `queued`, `running`, `succeeded`, `failed`, `cancelled`.
- Task: retain existing statuses; add `blocked` for tasks that will not run because an ancestor failed or was cancelled.
- A workflow is `succeeded` only when every task succeeded.
- A workflow is `failed` when any task fails or becomes blocked.
- A workflow is `cancelled` when explicitly cancelled before terminal failure/success. Running tasks receive the existing cancellation request; queued tasks are cancelled immediately.
- A workflow is `running` once at least one member task starts; otherwise it remains `queued`.

### Scheduling

- A workflow task is eligible only when it is queued, its scheduled `available_at` has arrived, and every direct dependency succeeded.
- Claiming remains atomic and preserves priority/FIFO ordering among eligible tasks.
- Tasks with unresolved dependencies are not claimable.
- When a task reaches a terminal state, reconcile its downstream tasks and workflow:
  - success: release children whose complete dependency set has succeeded;
  - failure/cancellation: mark not-yet-started descendants `blocked`, recording the failed ancestor(s);
  - never replay a running task after worker restart.
- Existing retry and bounded repair semantics remain per task. A task with retries scheduled is not terminal and must not release dependents.

### API

- `POST /workflows`: create a workflow and all member tasks atomically. Request contains workflow title and task definitions with stable client keys plus dependency key lists. Validate unique keys, dependency references, no self-dependency, no cycles, workspace paths, and existing task field constraints before committing.
- `GET /workflows`: list workflows with summary counts.
- `GET /workflows/{workflow_id}`: workflow, member tasks, and dependency edges.
- `POST /workflows/{workflow_id}/cancel`: request cancellation for running tasks and cancel queued tasks.
- Existing `POST /tasks` remains backward compatible for standalone tasks.

### Dashboard

- Add a workflow creation surface with task rows and dependency selection.
- Show workflow status and counts.
- Show task dependencies in the workflow detail view, including blocked reason.
- Keep existing standalone task creation and task detail behavior.

## Safety and compatibility

- No permission-mode changes, no permission bypass, and no implicit auto-repair.
- Existing standalone tasks remain claimable and behave as before.
- Workflow creation is all-or-nothing; invalid DAGs must not leave partial rows.
- Dependencies are stored relationally with foreign keys and indexed lookup paths.
- SQLite schema changes are additive and compatible with existing local databases.
- Worker reconciliation must be idempotent and safe under concurrent claim attempts.
- Do not claim OS-level sandboxing; trusted workspaces and trusted verification commands remain required.

## Test acceptance criteria

- API tests for valid workflow creation, malformed dependency references, duplicate keys, self-dependency, cycles, and atomic rollback.
- Worker tests proving children cannot be claimed early and become eligible only after all parents succeed.
- Failure/cancellation propagation tests for descendants, including multi-parent dependencies.
- Retry and repair tests proving dependents are not released until the upstream task is truly terminal-success.
- Workflow status aggregation tests for success, failure, blocked, and cancellation.
- Existing standalone task, priority, retry, cancellation, verification, repair, and orphan-recovery tests remain green.
