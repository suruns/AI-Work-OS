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
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "PASS: 受控执行审批闭环" in result.stdout


def test_controlled_execution_acceptance_script_is_valid_python():
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "acceptance" / "controlled_execution.py"
    compile(script.read_text(encoding="utf-8"), str(script), "exec")


def test_phase_11_5_acceptance_shell_script_is_valid_and_enables_real_checks():
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "acceptance" / "phase-11-5.sh"
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    source = script.read_text(encoding="utf-8")
    assert "AI_WORK_OS_RUN_CLAUDE_MCP_ACCEPTANCE=1" in source
    assert "AI_WORK_OS_RUN_BUBBLEWRAP_MCP_ACCEPTANCE=1" in source
    assert "AI_WORK_OS_RUN_CONTROLLED_EXECUTION_ACCEPTANCE=1" in source
    assert 'git diff --check origin/main...HEAD' in source
    assert 'for attempt in 1 2 3' in source
