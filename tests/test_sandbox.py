from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_work_os import sandbox


def _task_dir(tmp_path: Path) -> Path:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "mcp-config.isolated.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    return task_dir


def test_sandbox_off_preserves_legacy_execution(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "off")
    assert sandbox.build_sandbox_invocation(
        [sys.executable, "-V"], tmp_path, _task_dir(tmp_path), "plan", {}
    ) is None


def test_invalid_sandbox_mode_fails_closed(monkeypatch):
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "disabled-ish")
    with pytest.raises(sandbox.SandboxError, match="AI_WORK_OS_SANDBOX_MODE"):
        sandbox.sandbox_mode()


def test_bubblewrap_plan_builds_read_only_workspace_and_no_host_home(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_dir = _task_dir(tmp_path)
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "bubblewrap")
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_NETWORK", "none")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    with patch.object(sandbox, "_probe_bwrap"), patch.object(sandbox, "_probe_invocation"), patch.object(sandbox.shutil, "which", return_value=sys.executable):
        invocation = sandbox.build_sandbox_invocation(
            [sys.executable, "-V"],
            workspace,
            task_dir,
            "plan",
            {"PATH": os.environ.get("PATH", ""), "ANTHROPIC_API_KEY": "secret",
             "ANTHROPIC_MODEL": "must-not-pass", "HOME": "/home/operator"},
        )
    assert invocation is not None
    assert "--unshare-net" in invocation.command
    assert invocation.command[invocation.command.index("--ro-bind") + 2] != "/"
    assert ["--ro-bind", str(workspace), "/workspace"] == [
        invocation.command[i:i + 3] for i, value in enumerate(invocation.command[:-2])
        if value == "--ro-bind" and invocation.command[i + 2] == "/workspace"
    ][0]
    assert "/home/operator" not in invocation.command
    assert invocation.env["ANTHROPIC_API_KEY"] == "secret"
    assert "ANTHROPIC_MODEL" not in invocation.env
    assert invocation.env["HOME"] == "/tmp/awos-home"
    assert invocation.metadata["workspace_access"] == "read_only"
    assert invocation.metadata["network"] == "none"
    assert invocation.cwd == "/"
    assert "/awos/task/mcp-config.isolated.json" in invocation.command
    assert str(task_dir) in invocation.command
    mcp_index = invocation.command.index("--mcp-config")
    assert invocation.command[mcp_index + 1] == "/awos/task/mcp-config.isolated.json"
    assert ["--tmpfs", "/home"] in [
        invocation.command[i:i + 2] for i in range(len(invocation.command) - 1)
    ]
    assert ["--ro-bind", "/", "/"] not in [
        invocation.command[i:i + 3] for i in range(len(invocation.command) - 2)
    ]


def test_configured_mode_binds_workspace_read_write(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_dir = _task_dir(tmp_path)
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "bubblewrap")
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_NETWORK", "host")
    with patch.object(sandbox, "_probe_bwrap"), patch.object(sandbox, "_probe_invocation"), patch.object(sandbox.shutil, "which", return_value=sys.executable):
        invocation = sandbox.build_sandbox_invocation(
            [sys.executable, "-V"], workspace, task_dir, "configured", {"PATH": os.environ.get("PATH", "")}
        )
    assert invocation is not None
    assert "--unshare-net" not in invocation.command
    assert ["--bind", str(workspace), "/workspace"] == [
        invocation.command[i:i + 3] for i, value in enumerate(invocation.command[:-2])
        if value == "--bind" and invocation.command[i + 2] == "/workspace"
    ][0]
    assert invocation.metadata["workspace_access"] == "read_write"


def test_workspace_symlink_is_rejected(monkeypatch, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "workspace-link"
    link.symlink_to(target, target_is_directory=True)
    task_dir = _task_dir(tmp_path)
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "bubblewrap")
    with patch.object(sandbox, "_probe_bwrap"), patch.object(sandbox, "_probe_invocation"), patch.object(sandbox.shutil, "which", return_value=sys.executable):
        with pytest.raises(sandbox.SandboxError, match="must not be a symlink"):
            sandbox.build_sandbox_invocation(
                [sys.executable, "-V"], link, task_dir, "configured", {"PATH": os.environ.get("PATH", "")}
            )


def test_missing_bwrap_fails_closed(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_dir = _task_dir(tmp_path)
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "bubblewrap")
    monkeypatch.setenv("AI_WORK_OS_BWRAP_BIN", "missing-bwrap-binary")
    with patch.object(sandbox.shutil, "which", return_value=None):
        with pytest.raises(sandbox.SandboxError, match="not found"):
            sandbox.build_sandbox_invocation(
                [sys.executable, "-V"], workspace, task_dir, "configured", {"PATH": ""}
            )


def test_runtime_paths_must_be_absolute_and_exist(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_RUNTIME_PATHS", "relative/path")
    with pytest.raises(sandbox.SandboxError, match="absolute"):
        sandbox._extra_runtime_paths()
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_RUNTIME_PATHS", str(tmp_path / "missing"))
    with pytest.raises(sandbox.SandboxError, match="does not exist"):
        sandbox._extra_runtime_paths()


def test_runtime_file_is_rejected_to_avoid_mounting_its_parent(monkeypatch, tmp_path):
    runtime_file = tmp_path / "token-like-file"
    runtime_file.write_text("not a runtime directory", encoding="utf-8")
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_RUNTIME_PATHS", str(runtime_file))
    with pytest.raises(sandbox.SandboxError, match="existing directory"):
        sandbox._extra_runtime_paths()



def test_host_root_workspace_is_rejected(monkeypatch, tmp_path):
    task_dir = _task_dir(tmp_path)
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "bubblewrap")
    with patch.object(sandbox, "_probe_bwrap"), patch.object(sandbox, "_probe_invocation"), patch.object(sandbox.shutil, "which", return_value=sys.executable):
        with pytest.raises(sandbox.SandboxError, match="host system"):
            sandbox.build_sandbox_invocation(
                [sys.executable, "-V"], Path("/"), task_dir, "configured", {"PATH": os.environ.get("PATH", "")}
            )


def test_workspace_must_not_contain_task_storage(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    task_dir = workspace / "storage" / "tasks" / "task-1"
    task_dir.mkdir(parents=True)
    (task_dir / "mcp-config.isolated.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "bubblewrap")
    with patch.object(sandbox, "_probe_bwrap"), patch.object(sandbox, "_probe_invocation"), patch.object(sandbox.shutil, "which", return_value=sys.executable):
        with pytest.raises(sandbox.SandboxError, match="task artifact directory"):
            sandbox.build_sandbox_invocation(
                [sys.executable, "-V"], workspace, task_dir, "configured", {"PATH": os.environ.get("PATH", "")}
            )


def test_workspace_executable_symlink_cannot_expose_host_runtime(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    executable = outside / "run"
    executable.write_text("#!/bin/sh\\necho unsafe\\n", encoding="utf-8")
    executable.chmod(0o755)
    (workspace / "run").symlink_to(executable)
    task_dir = _task_dir(tmp_path)
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "bubblewrap")
    with patch.object(sandbox, "_probe_bwrap"), patch.object(sandbox, "_probe_invocation"), patch.object(sandbox.shutil, "which", return_value=sys.executable):
        with pytest.raises(sandbox.SandboxError, match="symlink points outside"):
            sandbox.build_sandbox_invocation(
                ["./run"], workspace, task_dir, "configured", {"PATH": os.environ.get("PATH", "")}
            )



def test_workspace_venv_shebang_is_rewritten_to_sandbox_path(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    bin_dir = workspace / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    interpreter = bin_dir / "python"
    interpreter.symlink_to(sys.executable)
    script = workspace / "run"
    script.write_text(f"#!{interpreter}\\nprint('ok')\\n", encoding="utf-8")
    script.chmod(0o755)
    task_dir = _task_dir(tmp_path)
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "bubblewrap")
    with patch.object(sandbox, "_probe_bwrap"), patch.object(sandbox, "_probe_invocation"), patch.object(sandbox.shutil, "which", return_value=sys.executable):
        invocation = sandbox.build_sandbox_invocation(
            ["./run"], workspace, task_dir, "configured", {"PATH": os.environ.get("PATH", "")}
        )
    assert invocation is not None
    separator = invocation.command.index("--")
    assert invocation.command[separator + 1] == "/workspace/.venv/bin/python"
    assert invocation.command[separator + 2] == "/workspace/run"


def test_claude_node_runtime_and_package_are_mounted_read_only(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_dir = _task_dir(tmp_path)
    runtime = tmp_path / "node-runtime"
    bin_dir = runtime / "bin"
    package_dir = runtime / "lib" / "node_modules" / "@anthropic-ai" / "claude-code"
    bin_dir.mkdir(parents=True)
    package_dir.mkdir(parents=True)
    (bin_dir / "node").symlink_to(sys.executable)
    cli_file = package_dir / "cli.js"
    cli_file.write_text("#!/usr/bin/env node\\n", encoding="utf-8")
    cli_file.chmod(0o755)
    claude_link = bin_dir / "claude"
    claude_link.symlink_to(cli_file)
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "bubblewrap")
    real_which = sandbox.shutil.which

    def fake_which(name, path=None):
        if name == "bwrap":
            return sys.executable
        return real_which(name, path=path)

    with patch.object(sandbox, "_probe_bwrap"), patch.object(sandbox, "_probe_invocation"), patch.object(sandbox.shutil, "which", side_effect=fake_which):
        invocation = sandbox.build_sandbox_invocation(
            [str(claude_link), "-p", "hello", "--mcp-config", str(task_dir / "mcp-config.isolated.json")],
            workspace, task_dir, "plan", {"PATH": str(bin_dir), "ANTHROPIC_API_KEY": "secret"}
        )
    assert invocation is not None
    assert str(bin_dir) in invocation.metadata["runtime_mounts"]
    assert str(package_dir) in invocation.metadata["runtime_mounts"]
    separator = invocation.command.index("--")
    assert invocation.command[separator + 1] == str(claude_link)
    mcp_index = invocation.command.index("--mcp-config")
    assert invocation.command[mcp_index + 1] == "/awos/task/mcp-config.isolated.json"
