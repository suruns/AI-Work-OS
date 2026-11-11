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
    with patch.object(sandbox, "_probe_bwrap"):
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


def test_configured_mode_binds_workspace_read_write(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_dir = _task_dir(tmp_path)
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "bubblewrap")
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_NETWORK", "host")
    with patch.object(sandbox, "_probe_bwrap"):
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
    with patch.object(sandbox, "_probe_bwrap"):
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
