import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(
    os.getenv("AI_WORK_OS_RUN_CONTROLLED_EXECUTION_ACCEPTANCE") != "1"
    or not os.getenv("AI_WORK_OS_CLAUDE_MODEL", "").strip(),
    reason=(
        "set AI_WORK_OS_RUN_CONTROLLED_EXECUTION_ACCEPTANCE=1 and "
        "AI_WORK_OS_CLAUDE_MODEL to a gateway-supported model"
    ),
)
def test_real_controlled_write_requires_approval_and_runs_under_bubblewrap():
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "acceptance" / "controlled_execution.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=600,
        check=False,
    )
    assert result.returncode == 0, result.stdout + "\\n" + result.stderr
    assert "PASS: 受控执行审批闭环" in result.stdout


def test_controlled_execution_acceptance_script_is_valid_python():
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "acceptance" / "controlled_execution.py"
    compile(script.read_text(encoding="utf-8"), str(script), "exec")


def test_phase_11_5_acceptance_shell_script_is_valid_and_scopes_runtime_environment():
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "acceptance" / "phase-11-5.sh"
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    source = script.read_text(encoding="utf-8")

    # Normal tests must never inherit the real-execution switches or Bubblewrap.
    assert 'AI_WORK_OS_SANDBOX_MODE=off' in source
    assert '-u AI_WORK_OS_RUN_CLAUDE_MCP_ACCEPTANCE' in source
    assert '-u AI_WORK_OS_RUN_BUBBLEWRAP_MCP_ACCEPTANCE' in source
    assert '-u AI_WORK_OS_RUN_CONTROLLED_EXECUTION_ACCEPTANCE' in source
    assert 'export AI_WORK_OS_SANDBOX_MODE=bubblewrap' not in source

    # Real acceptance switches are scoped to the dedicated acceptance pytest run.
    assert 'AI_WORK_OS_RUN_CLAUDE_MCP_ACCEPTANCE=1' in source
    assert 'AI_WORK_OS_RUN_BUBBLEWRAP_MCP_ACCEPTANCE=1' in source
    assert 'AI_WORK_OS_RUN_CONTROLLED_EXECUTION_ACCEPTANCE=1' in source
    assert 'tests/test_master_agent_cli_acceptance.py' in source
    assert 'tests/test_master_agent_bubblewrap_acceptance.py' in source
    assert 'tests/test_execution_approval_acceptance.py' in source
    assert 'git diff --check origin/main...HEAD' in source
    assert 'for attempt in 1 2 3' in source
    assert 'python -m playwright install chromium' not in source


def test_e2e_extra_pins_playwright_to_the_preinstalled_browser_revision():
    root = Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'e2e = ["playwright==1.64.1"]' in pyproject
