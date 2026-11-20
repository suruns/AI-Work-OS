#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${AI_WORK_OS_PYTHON:-python}"
TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/awos-phase-11-6.XXXXXX")"
cleanup() {
  rm -rf "$TEMP_ROOT"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p "$TEMP_ROOT/storage"
export AI_WORK_OS_DB="sqlite:///$TEMP_ROOT/phase-11-6.db"
export AI_WORK_OS_STORAGE="$TEMP_ROOT/storage"
export AI_WORK_OS_SANDBOX_MODE=off
unset AI_WORK_OS_RUN_CLAUDE_MCP_ACCEPTANCE
unset AI_WORK_OS_RUN_BUBBLEWRAP_MCP_ACCEPTANCE
unset AI_WORK_OS_RUN_CONTROLLED_EXECUTION_ACCEPTANCE
unset AI_WORK_OS_RUN_MASTER_GOAL_ACCEPTANCE

echo "[1/2] MasterRun 数据模型、状态、预算与自动续跑"
"$PYTHON_BIN" -m pytest -q \
  tests/test_master_run.py \
  tests/test_master_run_continuation.py

echo "[2/2] Master Agent MCP、审批/取消、Chat API 与项目证据"
"$PYTHON_BIN" -m pytest -q \
  tests/test_mcp_control_plane.py \
  tests/test_api.py

echo "[3/3] Master Agent turn 队列、租约、恢复与 MCP broker"
"$PYTHON_BIN" -m pytest -q tests/test_master_agent.py

echo "PASS：Phase 11.6 目标编排专项验收通过。"
echo "注意：真实 Claude CLI + Bubblewrap + 端到端工作场景仍需结合 phase-11-5.sh 的环境预检与验收流程执行。"
