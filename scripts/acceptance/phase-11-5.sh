#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

CLAUDE_BIN="${AI_WORK_OS_CLAUDE_BIN:-claude}"
BWRAP_BIN="${AI_WORK_OS_BWRAP_BIN:-bwrap}"

if [[ "$(id -u)" == "0" ]]; then
  echo "错误：Bubblewrap Worker 必须以普通用户运行，不能以 root 运行。" >&2
  exit 2
fi
if ! command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
  echo "错误：找不到 Claude Code CLI：$CLAUDE_BIN" >&2
  exit 2
fi
if ! command -v "$BWRAP_BIN" >/dev/null 2>&1; then
  echo "错误：找不到 Bubblewrap：$BWRAP_BIN" >&2
  exit 2
fi
if [[ -z "${AI_WORK_OS_CLAUDE_MODEL:-}" ]]; then
  echo "错误：请设置 AI_WORK_OS_CLAUDE_MODEL 为模型网关支持的模型名。" >&2
  exit 2
fi

export AI_WORK_OS_SANDBOX_MODE=bubblewrap
export AI_WORK_OS_SANDBOX_NETWORK="${AI_WORK_OS_SANDBOX_NETWORK:-host}"
export AI_WORK_OS_RUN_CLAUDE_MCP_ACCEPTANCE=1
export AI_WORK_OS_RUN_BUBBLEWRAP_MCP_ACCEPTANCE=1
export AI_WORK_OS_RUN_CONTROLLED_EXECUTION_ACCEPTANCE=1

python -m playwright install chromium

git fetch origin main:refs/remotes/origin/main
git diff --check origin/main...HEAD

TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/awos-phase-11-5-acceptance.XXXXXX")"
cleanup() {
  rm -rf "$TEMP_ROOT"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -p "$TEMP_ROOT/storage"
export AI_WORK_OS_DB="sqlite:///$TEMP_ROOT/automated-tests.db"
export AI_WORK_OS_STORAGE="$TEMP_ROOT/storage"

echo "[1/2] 运行全部 API/单元测试与真实 CLI/MCP/Bubblewrap/受控写入验收"
python -m pytest -q -m "not e2e"

echo "[2/2] 连续运行 Dashboard 浏览器 E2E 三轮"
for attempt in 1 2 3; do
  echo "Dashboard E2E 验收：第 $attempt/3 轮"
  python -m pytest -q tests/e2e -m e2e
done

echo "PASS：Phase 11.5 本机集中验收全部通过。"
