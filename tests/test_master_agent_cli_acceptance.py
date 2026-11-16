import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(
    os.getenv("AI_WORK_OS_RUN_CLAUDE_MCP_ACCEPTANCE") != "1"
    or not os.getenv("AI_WORK_OS_CLAUDE_MODEL", "").strip(),
    reason=(
        "set AI_WORK_OS_RUN_CLAUDE_MCP_ACCEPTANCE=1 and "
        "AI_WORK_OS_CLAUDE_MODEL to a gateway-supported model"
    ),
)
def test_real_claude_cli_can_call_scoped_mcp_tools_and_queue_work():
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "acceptance" / "master_mcp_cli.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=330,
        check=False,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "PASS: real Claude Code MCP calls" in result.stdout
