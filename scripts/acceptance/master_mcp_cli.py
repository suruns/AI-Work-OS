from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _walk(value):
    if isinstance(value, dict):
        if value.get("type") == "tool_use" and isinstance(value.get("name"), str):
            yield value["name"]
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "src"))
    claude_bin = os.getenv("AI_WORK_OS_CLAUDE_BIN", "claude")
    if shutil.which(claude_bin) is None:
        print("FAIL: Claude Code CLI is not available on PATH")
        return 2

    with tempfile.TemporaryDirectory(prefix="awos-real-mcp-cli-") as temp:
        temp_path = Path(temp)
        workspace = temp_path / "workspace"
        workspace.mkdir()
        storage = temp_path / "storage"
        storage.mkdir()
        db_path = temp_path / "awos.db"
        os.environ["AI_WORK_OS_DB"] = f"sqlite:///{db_path}"
        os.environ["AI_WORK_OS_STORAGE"] = str(storage)
        os.environ["AI_WORK_OS_SANDBOX_MODE"] = "off"

        from ai_work_os.db import Base, SessionLocal, engine
        from ai_work_os.models import Project, Task, Worker, Workflow, WorkflowTaskDependency
        from ai_work_os.master_agent import _subprocess_env, _write_mcp_config, build_master_command

        Base.metadata.create_all(bind=engine)
        with SessionLocal() as db:
            project = Project(
                name="Real MCP CLI Acceptance",
                slug="real-mcp-cli-acceptance",
                workspace_path=str(workspace),
                status="active",
            )
            db.add(project)
            db.commit()
            project_id = project.id

        turn_dir = storage / "master-agent" / "acceptance"
        turn_dir.mkdir(parents=True)
        mcp_config = _write_mcp_config(turn_dir, project_id)
        claude_config = storage / "claude-config"
        claude_config.mkdir(mode=0o700)
        os.chmod(claude_config, 0o700)

        command = build_master_command(None, mcp_config)
        env = _subprocess_env()
        env["CLAUDE_CONFIG_DIR"] = str(claude_config)
        env["AI_WORK_OS_DB"] = f"sqlite:///{db_path}"
        env["AI_WORK_OS_STORAGE"] = str(storage)
        env["AI_WORK_OS_SANDBOX_MODE"] = "off"
        env["PYTHONPATH"] = os.pathsep.join(
            [str(root / "src"), env.get("PYTHONPATH", "")]
        )
        prompt = """Use the connected ai-work-os MCP tools now; do not merely describe actions.
Call get_project_overview and list_tasks first.
Then call create_task exactly once with title "Real CLI MCP task" and prompt
"Inspect the project structure and report; do not modify files."
Then call create_workflow exactly once with title "Real CLI MCP DAG" and three tasks:
- inspect: inspect the project structure
- implement: plan a minimal implementation, depends_on ["inspect"]
- verify: plan validation, depends_on ["implement"]
After all four tool calls succeed, briefly report the returned task and workflow IDs.
Do not use shell commands or file-editing tools."""

        try:
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                capture_output=True,
                cwd=workspace,
                env=env,
                timeout=300,
                check=False,
            )
        except subprocess.TimeoutExpired:
            print("FAIL: Claude Code CLI timed out after 300 seconds")
            return 1

        events = []
        for line in completed.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        called = set(name for event in events for name in _walk(event))
        required = {
            "mcp__awos__get_project_overview",
            "mcp__awos__list_tasks",
            "mcp__awos__create_task",
            "mcp__awos__create_workflow",
        }
        missing = sorted(required - called)
        with SessionLocal() as db:
            tasks = list(db.query(Task).filter(Task.project_id == project_id).all())
            workflows = list(db.query(Workflow).filter(Workflow.project_id == project_id).all())
            edges = list(db.query(WorkflowTaskDependency).all())
            workers = list(db.query(Worker).all())

        task_created = [task for task in tasks if task.title == "Real CLI MCP task"]
        workflow_created = [item for item in workflows if item.title == "Real CLI MCP DAG"]
        dag_tasks = [task for task in tasks if task.workflow_id == (workflow_created[0].id if workflow_created else None)]
        ok = (
            completed.returncode == 0
            and not missing
            and len(task_created) == 1
            and len(workflow_created) == 1
            and len(dag_tasks) == 3
            and len(edges) == 2
            and len(workers) == 0
            and all(task.status == "queued" and task.permission_mode == "plan" for task in tasks)
        )
        if not ok:
            print("FAIL: real MCP CLI acceptance did not satisfy all assertions")
            print(f"exit_code={completed.returncode}")
            print(f"missing_tool_calls={missing}")
            print(f"project_task_count={len(tasks)} workflow_count={len(workflows)}")
            if completed.stderr.strip():
                print("stderr_tail:")
                print(completed.stderr[-3000:])
            return 1
        print("PASS: real Claude Code MCP calls and persisted queued DAG")
        print("tools=" + ",".join(sorted(called & required)))
        print(f"project_tasks={len(tasks)} workflows={len(workflows)} dag_edges={len(edges)} workers={len(workers)}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
