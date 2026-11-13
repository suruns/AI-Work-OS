import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(
    os.getenv("AI_WORK_OS_RUN_BUBBLEWRAP_MCP_ACCEPTANCE") != "1"
    or not os.getenv("AI_WORK_OS_CLAUDE_MODEL", "").strip(),
    reason=(
        "set AI_WORK_OS_RUN_BUBBLEWRAP_MCP_ACCEPTANCE=1 and "
        "AI_WORK_OS_CLAUDE_MODEL to a gateway-supported model"
    ),
)
def test_real_claude_cli_project_mcp_runs_through_bubblewrap_broker():
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "acceptance" / "master_mcp_bubblewrap.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=600,
        check=False,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "PASS: Bubblewrap project MCP broker" in result.stdout


def test_bubblewrap_acceptance_script_is_valid_python():
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "acceptance" / "master_mcp_bubblewrap.py"
    compile(script.read_text(encoding="utf-8"), str(script), "exec")
