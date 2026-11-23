# Project Management UI — Design and Implementation Plan

## Goal

Make Projects a first-class workspace in the Chinese Chat-first AI Work OS UI. Users can create/select projects, inspect project status and associated tasks/workflows/goals, and start a Master Agent conversation with project context.

## Navigation

- Master Agent: global conversation, no project selected by default.
- 项目管理: project list and project detail workspace.
- 任务管理: global task inventory, filterable by project.
- 工作流: global workflow inventory.
- 执行审批: pending approvals.

The existing Dashboard remains available as an execution/diagnostics view; this phase does not replace or remove it.

## Project list

Each project card/row displays:
- Name and description
- Workspace root
- Active/archived status
- Last activity (derived from available timestamps; do not fabricate)
- Task counts by status (queued, running, succeeded, failed, cancelled)
- Workflow counts
- Active goal count when supported by API

Actions:
- Create project
- Open project
- Archive/reactivate project
- Start project-scoped Master Agent conversation

## Project detail

Tabs/sections:
1. Overview: metadata, workspace root, status, aggregate task/workflow counts.
2. Tasks: project-filtered tasks; status, title, created/updated, worker, result summary.
3. Workflows: project-filtered workflows; status and dependency progress.
4. Goals: project-linked Master Agent goals and evidence/report where APIs support project filtering.
5. Activity: chronological view from existing task/workflow/goal timestamps and events; only show data that APIs provide.

Primary action: “在此项目中与 Master Agent 对话”, navigating to Chat and selecting this project context.

## Create project

Fields:
- 项目名称 (required)
- 描述 (optional)
- 工作目录 (required; backend validates canonical existing directory)

Use existing POST /projects API. Slug generation remains server-side. On success, open the project detail page and refresh project inventory.

## API integration

Use existing project endpoints first:
- GET /projects
- POST /projects
- GET /projects/{id}
- PATCH /projects/{id}
- GET /projects/{id}/tasks
- GET /projects/{id}/workflows
- Existing global task/workflow endpoints with project_id filters

Inspect goal APIs before implementing project goals; avoid adding duplicate backend concepts. Add API endpoints only if current endpoints cannot support the UI.

## UI architecture

- Chinese (zh-CN) labels and messages.
- Keep existing Chat-first experience intact.
- Add a project workspace route/view within current frontend rather than a second app.
- Reuse current design tokens, cards, badges, responsive layout.
- Project selection is explicit; global chat must not silently inherit a stale project.
- Handle loading, empty, error, archived, and partial-data states.
- No destructive project deletion. Archive is reversible.

## Acceptance

- Project list loads and displays real backend data.
- Create project validation and successful creation.
- Project detail metadata and counts match API.
- Task/workflow lists are correctly scoped to project.
- Archive/reactivate behavior is reflected in UI.
- Project-scoped chat selects correct project context and persists through conversation switching.
- Existing Chat, Dashboard, task, workflow, approval, and goal functionality remains regression-free.
- Responsive layout at desktop and narrow widths.
- E2E tests cover list, create, detail, project filtering, and chat-context navigation.
