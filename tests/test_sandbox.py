from __future__ import annotations

from pathlib import Path

import pytest

from ai_work_os.sandbox import _nested_mountpoints, build_sandbox_invocation


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
    cli.chmod(0o755)
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
    claude, node = _fake_claude_runtime(workspace / "runtime")
    env = {"PATH": f"{claude.parent}:{fake_bin}", "ANTHROPIC_API_KEY": "do-not-log-this",
           "ANTHROPIC_BASE_URL": "https://api.example.invalid", "ANTHROPIC_MODEL": "should-not-pass",
           "CLAUDE_CODE_OAUTH_TOKEN": "oauth-secret",
           "HOME": str(tmp_path / "real-home")}
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "bubblewrap")
    monkeypatch.setenv("AI_WORK_OS_BWRAP_BIN", str(bwrap))
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_NETWORK", "none")
    result = build_sandbox_invocation([str(claude), "-p", "inspect"], workspace, task_dir, "plan", env)
    command = result.command
    assert "--unshare-user" in command and "--unshare-pid" in command and "--unshare-net" in command
    assert "--disable-userns" in command and "--uid" in command and "--gid" in command
    assert ["--perms", "01777", "--tmpfs"] == [
        command[i:i+3] for i in range(len(command)-2) if command[i] == "--perms"
    ][0]
    assert ["--chmod", "0777", "/tmp/home"] in [
        command[i:i+3] for i in range(len(command)-2) if command[i] == "--chmod"
    ]
    ro_binds = [command[i:i+3] for i in range(len(command)-2) if command[i] == "--ro-bind"]
    assert ["--ro-bind", str(workspace.resolve()), "/workspace"] in ro_binds
    assert str(node.resolve()) not in command  # runtime is already inside the workspace mount
    assert "/workspace/runtime/node-bin" in result.env["PATH"]
    assert "do-not-log-this" not in command and "should-not-pass" not in command
    assert "oauth-secret" not in command
    assert result.env["ANTHROPIC_API_KEY"] == "do-not-log-this"
    assert result.env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-secret"
    assert str(tmp_path / "real-home") not in command
    assert result.cwd == "/" and result.metadata["workspace_access"] == "read_only"


def test_bubblewrap_maps_only_mcp_file_and_uses_rw_workspace(tmp_path, monkeypatch):
    workspace, task_dir, fake_bin = tmp_path / "workspace", tmp_path / "task", tmp_path / "bin"
    workspace.mkdir(); task_dir.mkdir(); fake_bin.mkdir()
    bwrap = fake_bin / "bwrap"; bwrap.write_text("#!/bin/sh\nexit 0\n"); bwrap.chmod(0o755)
    claude, _node = _fake_claude_runtime(workspace / "runtime")
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
    assert "auth" not in command and result.env["ANTHROPIC_AUTH_TOKEN"] == "auth"


def test_bubblewrap_fails_closed_for_workspace_symlink_and_missing_bwrap(tmp_path, monkeypatch):
    real = tmp_path / "real"; real.mkdir()
    link = tmp_path / "workspace-link"; link.symlink_to(real, target_is_directory=True)
    task_dir = tmp_path / "task"; task_dir.mkdir()
    fake_bin = tmp_path / "bin"; fake_bin.mkdir()
    bwrap = fake_bin / "bwrap"; bwrap.write_text("#!/bin/sh\nexit 0\n"); bwrap.chmod(0o755)
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "bubblewrap")
    monkeypatch.setenv("AI_WORK_OS_BWRAP_BIN", "/missing/bwrap")
    with pytest.raises(FileNotFoundError, match="Bubblewrap"):
        build_sandbox_invocation(["true"], real, task_dir, "plan", {})
    monkeypatch.setenv("AI_WORK_OS_BWRAP_BIN", str(bwrap))
    with pytest.raises(ValueError, match="canonical"):
        build_sandbox_invocation(["true"], link, task_dir, "plan", {})



def test_task_artifacts_nested_in_workspace_are_hidden(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    task_dir = workspace / ".ai-work-os" / "tasks" / "task-1"
    fake_bin = tmp_path / "bin"
    task_dir.mkdir(parents=True)
    fake_bin.mkdir()
    bwrap = fake_bin / "bwrap"
    bwrap.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    bwrap.chmod(0o755)
    claude, _node = _fake_claude_runtime(workspace / "runtime")
    config = task_dir / "mcp-config.isolated.json"
    config.write_text('{"mcpServers":{}}\n', encoding="utf-8")
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "bubblewrap")
    monkeypatch.setenv("AI_WORK_OS_BWRAP_BIN", str(bwrap))
    result = build_sandbox_invocation(
        [str(claude), "--mcp-config", str(config)], workspace, task_dir, "plan",
        {"PATH": f"{claude.parent}:{fake_bin}"},
        mcp_config_path=config, execution_kind="task",
    )
    tmpfs_mounts = [
        result.command[i:i+2] for i in range(len(result.command)-1)
        if result.command[i] == "--tmpfs"
    ]
    assert ["--tmpfs", "/workspace/.ai-work-os/tasks/task-1"] in tmpfs_mounts
    assert result.metadata["task_artifacts_hidden_at"] == "/workspace/.ai-work-os/tasks/task-1"
    assert "/awos/task/mcp-config.isolated.json" in result.command



def test_nested_mountpoint_detection_ignores_workspace_mount_itself():
    workspace = Path("/work/project")
    mountinfo = (
        "30 20 0:25 / /work/project rw,relatime - ext4 /dev/sda1 rw\n"
        "31 30 0:26 / /work/project/.private rw,relatime - tmpfs tmpfs rw\n"
        "32 20 0:27 / /work/other rw,relatime - ext4 /dev/sda2 rw\n"
    )
    assert _nested_mountpoints(workspace, mountinfo) == ["/work/project/.private"]



def test_mcp_config_argv_is_rewritten_to_sandbox_mount(tmp_path, monkeypatch):
    workspace, task_dir, fake_bin = tmp_path / "workspace", tmp_path / "task", tmp_path / "bin"
    workspace.mkdir(); task_dir.mkdir(); fake_bin.mkdir()
    bwrap = fake_bin / "bwrap"
    bwrap.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    bwrap.chmod(0o755)
    claude, _node = _fake_claude_runtime(workspace / "runtime")
    config = task_dir / "mcp-config.isolated.json"
    config.write_text('{"mcpServers":{}}\n', encoding="utf-8")
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "bubblewrap")
    monkeypatch.setenv("AI_WORK_OS_BWRAP_BIN", str(bwrap))
    result = build_sandbox_invocation(
        [str(claude), "--mcp-config", str(config)], workspace, task_dir,
        "configured", {"PATH": f"{claude.parent}:{fake_bin}"},
        mcp_config_path=config, execution_kind="task",
    )
    index = result.command.index("--mcp-config")
    assert result.command[index + 1] == "/awos/task/mcp-config.isolated.json"
    command_separator = result.command.index("--")
    claude_argv = result.command[command_separator + 1:]
    assert str(config.resolve()) not in claude_argv
    assert ["--ro-bind", str(config.resolve()), "/awos/task/mcp-config.isolated.json"] == [
        result.command[index:index + 3]
        for index, argument in enumerate(result.command[:-2])
        if argument == "--ro-bind"
    ][-1]


def test_runtime_mounts_below_tmp_survive_tmpfs_creation(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    task_dir = tmp_path / "task"
    runtime = tmp_path / "runtime"
    fake_bin = tmp_path / "bin"
    workspace.mkdir(); task_dir.mkdir(); fake_bin.mkdir()
    claude, node = _fake_claude_runtime(runtime)
    bwrap = fake_bin / "bwrap"
    bwrap.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    bwrap.chmod(0o755)
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "bubblewrap")
    monkeypatch.setenv("AI_WORK_OS_BWRAP_BIN", str(bwrap))
    env = {"PATH": f"{node.parent}:{claude.parent}:{fake_bin}"}
    result = build_sandbox_invocation(
        [str(claude), "-p", "probe"], workspace, task_dir, "plan", env,
        execution_kind="task",
    )
    tmpfs_index = result.command.index("--tmpfs")
    runtime_bind_index = result.command.index("--ro-bind", tmpfs_index)
    assert result.command[tmpfs_index + 1] == "/tmp"
    assert runtime_bind_index > tmpfs_index
    assert str(node.resolve()) in result.command
    assert str(claude.resolve().parent) in result.command


def test_bubblewrap_rejects_unknown_permission_mode(tmp_path, monkeypatch):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    bwrap = fake_bin / "bwrap"
    bwrap.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    bwrap.chmod(0o755)
    workspace = tmp_path / "workspace"
    task_dir = tmp_path / "task"
    workspace.mkdir()
    task_dir.mkdir()
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "bubblewrap")
    monkeypatch.setenv("AI_WORK_OS_BWRAP_BIN", str(bwrap))
    with pytest.raises(ValueError, match="unsupported sandbox permission mode"):
        build_sandbox_invocation(
            ["/usr/bin/true"], workspace, task_dir, "manual", None,
            execution_kind="verification",
        )


def test_bubblewrap_persists_only_the_isolated_claude_session_directory(tmp_path, monkeypatch):
    workspace, task_dir = tmp_path / "workspace", tmp_path / "task"
    state_dir, fake_bin = tmp_path / "session-state", tmp_path / "bin"
    workspace.mkdir(); task_dir.mkdir(); state_dir.mkdir(); fake_bin.mkdir()
    bwrap = fake_bin / "bwrap"
    bwrap.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    bwrap.chmod(0o755)
    claude, _node = _fake_claude_runtime(workspace / "runtime")
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "bubblewrap")
    monkeypatch.setenv("AI_WORK_OS_BWRAP_BIN", str(bwrap))
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_NETWORK", "host")
    env = {
        "PATH": f"{claude.parent}:{fake_bin}",
        "ANTHROPIC_API_KEY": "auth-secret",
        "CLAUDE_CONFIG_DIR": str(state_dir),
    }
    result = build_sandbox_invocation(
        [str(claude), "--print"], workspace, task_dir, "plan", env,
        claude_config_dir=state_dir,
    )
    binds = [
        result.command[i:i+3]
        for i in range(len(result.command)-2)
        if result.command[i] == "--bind"
    ]
    assert ["--bind", str(state_dir.resolve()), "/tmp/home/.claude"] in binds
    assert result.env["CLAUDE_CONFIG_DIR"] == "/tmp/home/.claude"
    assert result.env["ANTHROPIC_API_KEY"] == "auth-secret"
    assert result.metadata["persistent_claude_config"] is True
    assert str(state_dir.resolve()) not in result.command[result.command.index("--") + 1:]


def test_bubblewrap_rejects_claude_session_directory_inside_workspace(tmp_path, monkeypatch):
    workspace, task_dir = tmp_path / "workspace", tmp_path / "task"
    state_dir, fake_bin = workspace / ".sessions", tmp_path / "bin"
    workspace.mkdir(); task_dir.mkdir(); state_dir.mkdir(); fake_bin.mkdir()
    bwrap = fake_bin / "bwrap"
    bwrap.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    bwrap.chmod(0o755)
    claude, _node = _fake_claude_runtime(workspace / "runtime")
    monkeypatch.setenv("AI_WORK_OS_SANDBOX_MODE", "bubblewrap")
    monkeypatch.setenv("AI_WORK_OS_BWRAP_BIN", str(bwrap))
    with pytest.raises(ValueError, match="outside workspace"):
        build_sandbox_invocation(
            [str(claude), "--print"], workspace, task_dir, "plan",
            {"PATH": f"{claude.parent}:{fake_bin}"},
            claude_config_dir=state_dir,
        )
