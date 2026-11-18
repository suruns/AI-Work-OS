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

# 只检查浏览器，不在验收过程中隐式下载 Chromium。
# Playwright 与 Chromium revision 必须由锁定的依赖版本共同决定。
python - <<'PY'
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys

expected = "1.64.1"
try:
    installed = version("playwright")
except PackageNotFoundError:
    installed = "未安装"

if installed != expected:
    print(
        f"错误：Playwright 版本必须为 {expected}，当前为 {installed}。"
        '请在当前虚拟环境运行：pip install -e ".[dev,e2e]"',
        file=sys.stderr,
    )
    raise SystemExit(2)

from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    executable = Path(playwright.chromium.executable_path)
    if not executable.is_file():
        print(
            "错误：未找到与 Playwright 1.64.1 匹配的 Chromium："
            f"{executable}\n请在可访问浏览器下载源的环境中运行 "
            "'python -m playwright install chromium'，"
            "或准备好对应的 Chromium 缓存后重新验收。",
            file=sys.stderr,
        )
        raise SystemExit(2)

print(f"浏览器预检通过：Playwright {expected}，Chromium={executable}")
PY

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

echo "[1/3] 运行 API/单元测试（明确关闭沙箱与真实模型验收）"
env \
  -u AI_WORK_OS_RUN_CLAUDE_MCP_ACCEPTANCE \
  -u AI_WORK_OS_RUN_BUBBLEWRAP_MCP_ACCEPTANCE \
  -u AI_WORK_OS_RUN_CONTROLLED_EXECUTION_ACCEPTANCE \
  -u AI_WORK_OS_SANDBOX_NETWORK \
  AI_WORK_OS_SANDBOX_MODE=off \
  python -m pytest -q -m "not e2e"

echo "[2/3] 运行真实 Claude CLI、Bubblewrap MCP 与受控写入验收"
env \
  AI_WORK_OS_SANDBOX_MODE=bubblewrap \
  AI_WORK_OS_SANDBOX_NETWORK="${AI_WORK_OS_SANDBOX_NETWORK:-host}" \
  AI_WORK_OS_RUN_CLAUDE_MCP_ACCEPTANCE=1 \
  AI_WORK_OS_RUN_BUBBLEWRAP_MCP_ACCEPTANCE=1 \
  AI_WORK_OS_RUN_CONTROLLED_EXECUTION_ACCEPTANCE=1 \
  python -m pytest -q \
    tests/test_master_agent_cli_acceptance.py \
    tests/test_master_agent_bubblewrap_acceptance.py \
    tests/test_execution_approval_acceptance.py

echo "[3/3] 连续运行 Dashboard 浏览器 E2E 三轮"
for attempt in 1 2 3; do
  echo "Dashboard E2E 验收：第 $attempt/3 轮"
  python -m pytest -q tests/e2e -m e2e
done

echo "PASS：Phase 11.5 本机集中验收全部通过。"
