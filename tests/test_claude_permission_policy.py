from ai_work_os.worker import build_command


def test_configured_mode_uses_non_interactive_file_edit_policy():
    command = build_command(
        "edit the requested file",
        "configured",
        setting_sources=["project", "local"],
    )
    index = command.index("--permission-mode")
    assert command[index + 1] == "acceptEdits"
    assert "--dangerously-skip-permissions" not in command
    assert "bypassPermissions" not in command
    assert command[command.index("--permission-prompts") + 1] == "none"
    disallowed = [command[i + 1] for i, value in enumerate(command[:-1]) if value == "--disallowedTools"]
    assert disallowed == ["mcp__*", "Bash"]


def test_plan_mode_remains_explicitly_read_only():
    command = build_command(
        "inspect only",
        "plan",
        setting_sources=["project", "local"],
    )
    index = command.index("--permission-mode")
    assert command[index + 1] == "plan"
