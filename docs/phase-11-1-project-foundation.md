# Phase 11.1 — Project foundation

## Status

Implementation in progress on `feat/phase-11-1-project-foundation`, stacked on the Phase 11 architecture proposal. This phase adds the backend project foundation only; it does not replace the Dashboard landing page or implement chat/conversations.

## Scope

- Add a durable Project record with name, unique slug, description, canonical workspace root, active/archived status, and timestamps.
- Add nullable `project_id` associations to Task and Workflow.
- Allow standalone Task and Workflow creation to remain backward compatible.
- Allow task/workflow creation to opt into a project association.
- Ensure every workflow member task inherits its workflow's project association.
- Add project CRUD/read APIs and project-scoped task/workflow inventory.
- Add project task/workflow status counts for project overview cards.
- Add additive SQLite compatibility migrations; existing rows remain unassigned (`project_id = NULL`).

## API

- `POST /projects`: create a project. Workspace root must exist. Slugs are normalized; non-Latin names receive a generated stable fallback slug.
- `GET /projects`: list projects, optionally filtered by `status=active|archived`.
- `GET /projects/{project_id}`: project metadata and task/workflow counts by status.
- `PATCH /projects/{project_id}`: update metadata, workspace root, or archive/reactivate.
- `GET /projects/{project_id}/tasks` and `GET /projects/{project_id}/workflows`: project-scoped inventories.
- `POST /tasks`: optional `project_id`; `GET /tasks?project_id=...` filters the global inventory.
- `POST /workflows`: optional `project_id`; all workflow tasks inherit it. `GET /workflows?project_id=...` filters the global inventory.

## Workspace and lifecycle rules

- Project workspace roots are canonical existing directories.
- A project-linked Task/Workflow may only target the project root or a directory beneath it.
- Archived projects cannot receive new tasks/workflows. Existing tasks continue to follow their normal Worker lifecycle.
- A project workspace root cannot be changed to a directory that excludes the workspace of any existing project task.
- Project association is organizational metadata, not a sandbox boundary. Bubblewrap and task permission enforcement remain authoritative.
- Project archive is preferred to deletion. This phase adds no project deletion endpoint and never cascades project deletion into execution history or artifacts.

## Compatibility

- Existing Task and Workflow endpoints remain available.
- New `project_id` fields are optional and default to null.
- Existing tasks/workflows are not automatically grouped into a guessed project.
- Task queue ordering, leases, retries, verification, repair, cancellation, execution events, artifacts, and sandbox behavior are unchanged.
- Worker imports the Project model before schema creation so the standalone Worker process can initialize the same schema as the API process.

## Acceptance

- Project create/list/detail/update and slug conflict behavior.
- Project-linked standalone task creation and project filtering.
- Project-linked workflow creation and inherited member-task association.
- Project summary counts match the underlying task/workflow states.
- Reject unknown/archived projects and workspaces outside the project root.
- Reject workspace-root changes that would orphan existing project task paths.
- Existing standalone task/workflow tests remain green.
- Existing API/unit tests and three-round Dashboard E2E suite remain green.
