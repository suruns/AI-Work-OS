# Phase 6.7 — Read-only Artifact Library

## Goal

Provide one place to discover outputs from recent tasks without changing the existing task-owned artifact model or introducing a file-management daemon.

## API

`GET /artifacts` scans the most recent tasks (default 100, maximum 500) and returns their existing task-owned logs/reports plus workspace files already listed in each task's workspace-change report.

Query parameters:

- `limit`: maximum returned items (1–500, default 100).
- `task_limit`: number of recent tasks to inspect (1–500, default 100).
- `task_id`: optional exact task filter; unknown task returns 404.
- `kind`: optional exact artifact kind filter.
- `q`: optional case-insensitive substring over artifact name/path, task title, workspace path, and task ID.

Each item includes the existing download URL plus task ID/title/status, workspace path, and task creation time. The response reports the number of matched items, scanned task count, and whether the item limit truncated the result.

## Dashboard

The Artifact library panel supports a manual search across recent task outputs and changed workspace files. It is intentionally not polled every five seconds: scanning workspace reports and checking current workspace files is more expensive than refreshing task status.

## Safety and limitations

- Read-only discovery only. No move, rename, delete, archive, or workspace write operation is provided.
- Download permissions are unchanged and remain scoped to existing task artifact and workspace-file endpoints.
- Workspace files are still derived from the persisted task change report and current filesystem state; deleted files and non-Git workspaces do not become downloadable historical snapshots.
- The inventory is bounded to the most recent `task_limit` tasks. Increase that parameter or filter by task ID when searching older history.
- The API remains intended for a trusted local developer machine; authentication and multi-user isolation are out of scope.
- No database table, migration, dependency, or background scanner is added.
