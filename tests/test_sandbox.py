from __future__ import annotations

from pathlib import Path

import pytest

from ai_work_os.sandbox import build_sandbox_invocation


def _fake_claude_runtime(root: Path) -> tuple[Path, Path]:
    node_bin = root / "node-bin"
    package = root / "node-modules" / "@anthropic-ai" / "claude-code"
    node_bin.mkdir(parents=True)
    package.mkdir(parents=True)
    node = node_bin / "node"
    node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    node.chmod(0o755)
    cli = package / "cli.js"
    cli.write_text("process.exit(0);\n", encoding="utf-8")
    link = node_bin / "claude"
    link.symlink_to(cli)
    return link, node


def test_sandbox_off_preserves_existing_execution(tmp_path, monkeypatch):
    workspace, task_dir = tmp_path / "workspace", tmp_path / "task"
    workspace.mkdir(); task_dir.mkdir()
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "off")
    env = {"ANTHROPIC_API_KEY": "secret"}
    result = build_sandbox_invocation(["claude", "-p", "inspect"], workspace, task_dir, "plan", env)
    assert result.command == ["claude", "-p", "inspect"]
    assert result.cwd == str(workspace) and result.env is env
    assert result.metadata["enabled"] is False


def test_bubblewrap_plan_is_read_only_and_network_is_disabled(tmp_path, monkeypatch):
    workspace, task_dir, fake_bin = tmp_path / "workspace", tmp_path / "task", tmp_path / "bin"
    workspace.mkdir(); task_dir.mkdir(); fake_bin.mkdir()
    bwrap = fake_bin / "bwrap"; bwrap.write_text("#!/bin/sh\nexit 0\n"); bwrap.chmod(0o755)
    claude, node = _fake_claude_runtime(tmp_path)
    env = {"PATH": f"{claude.parent}:{fake_bin}", "ANTHROPIC_API_KEY": "do-not-log-this",
           "ANTHROPIC_BASE_URL": "https://api.example.invalid", "ANTHROPIC_MODEL": "should-not-pass",
           "HOME": str(tmp_path / "real-home")}
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "bubblewrap")
    monkeypatch.setenv("AI_WORK_OS_BWRAP_BIN", str(bwrap))
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_NETWORK", "none")
    result = build_sandbox_invocation([str(claude), "-p", "inspect"], workspace, task_dir, "plan", env)
    command = result.command
    assert "--unshare-user" in command and "--unshare-pid" in command and "--unshare-net" in command
    ro_binds = [command[i:i+3] for i in range(len(command)-2) if command[i] == "--ro-bind"]
    assert ["--ro-bind", str(workspace.resolve()), "/workspace"] in ro_binds
    assert str(node.resolve()) in command and str(claude.resolve().parent) in command
    assert "do-not-log-this" in command and "should-not-pass" not in command
    assert str(tmp_path / "real-home") not in command
    assert result.cwd == "/" and result.metadata["workspace_access"] == "read_only"


def test_bubblewrap_maps_only_mcp_file_and_uses_rw_workspace(tmp_path, monkeypatch):
    workspace, task_dir, fake_bin = tmp_path / "workspace", tmp_path / "task", tmp_path / "bin"
    workspace.mkdir(); task_dir.mkdir(); fake_bin.mkdir()
    bwrap = fake_bin / "bwrap"; bwrap.write_text("#!/bin/sh\nexit 0\n"); bwrap.chmod(0o755)
    claude, _node = _fake_claude_runtime(tmp_path)
    config = task_dir / "mcp-config.isolated.json"; config.write_text('{"mcpServers":{}}\n')
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "bubblewrap")
    monkeypatch.setenv("AI_WORK_OS_BWRAP_BIN", str(bwrap))
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_NETWORK", "host")
    env = {"PATH": f"{claude.parent}:{fake_bin}", "ANTHROPIC_AUTH_TOKEN": "auth"}
    result = build_sandbox_invocation(
        [str(claude), "--mcp-config", str(config)], workspace, task_dir, "configured", env,
        mcp_config_path=config, execution_kind="repair",
    )
    command = result.command
    binds = [command[i:i+3] for i in range(len(command)-2) if command[i] == "--bind"]
    ro_binds = [command[i:i+3] for i in range(len(command)-2) if command[i] == "--ro-bind"]
    assert ["--bind", str(workspace.resolve()), "/workspace"] in binds
    assert "--unshare-net" not in command
    assert ["--ro-bind", str(config.resolve()), "/awos/task/mcp-config.isolated.json"] in ro_binds
    assert "/awos/task/mcp-config.isolated.json" in command
    assert str(task_dir) not in command and "auth" in command


def test_bubblewrap_fails_closed_for_workspace_symlink_and_missing_bwrap(tmp_path, monkeypatch):
    real = tmp_path / "real"; real.mkdir()
    link = tmp_path / "workspace-link"; link.symlink_to(real, target_is_directory=True)
    task_dir = tmp_path / "task"; task_dir.mkdir()
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "bubblewrap")
    monkeypatch.setenv("AI_WORK_OS_BWRAP_BIN", "/missing/bwrap")
    with pytest.raises(FileNotFoundError, match="Bubblewrap"):
        build_sandbox_invocation(["true"], real, task_dir, "plan", {})
    monkeypatch.setenv("AI_WORK_OS_BWRAP_BIN", "bwrap")
    with pytest.raises(ValueError, match="canonical"):
        build_sandbox_invocation(["true"], link, task_dir, "plan", {})
