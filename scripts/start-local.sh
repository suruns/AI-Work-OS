#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
fi

API_HOST="${AI_WORK_OS_API_HOST:-127.0.0.1}"
API_PORT="${AI_WORK_OS_API_PORT:-8000}"
case "$API_HOST" in
  127.0.0.1|localhost|::1) ;;
  *)
    echo "错误：受控执行模式只允许 API 绑定本机回环地址（127.0.0.1、localhost 或 ::1）。" >&2
    exit 2
    ;;
esac
URL_HOST="$API_HOST"
if [[ "$API_HOST" == "::1" ]]; then URL_HOST="[::1]"; fi
HEALTH_URL="http://${URL_HOST}:${API_PORT}/health"

api_pid=""
worker_pid=""
master_pid=""
cleaned_up=0

cleanup() {
  if [[ "$cleaned_up" == "1" ]]; then return; fi
  cleaned_up=1
  for pid in "$master_pid" "$worker_pid" "$api_pid"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  for pid in "$master_pid" "$worker_pid" "$api_pid"; do
    if [[ -n "$pid" ]]; then wait "$pid" 2>/dev/null || true; fi
  done
}
trap cleanup EXIT INT TERM

echo "[1/4] 启动 API：${API_HOST}:${API_PORT}"
uvicorn ai_work_os.api:app --host "$API_HOST" --port "$API_PORT" &
api_pid=$!

echo "[2/4] 等待 API 健康检查：$HEALTH_URL"
healthy=0
for _ in $(seq 1 60); do
  if curl --silent --show-error --fail "$HEALTH_URL" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  if ! kill -0 "$api_pid" 2>/dev/null; then
    echo "错误：API 在健康检查通过前退出。" >&2
    exit 1
  fi
  sleep 1
done
if [[ "$healthy" != "1" ]]; then
  echo "错误：等待 API 健康检查超时（60 秒）。Worker 和 Master Agent 未启动。" >&2
  exit 1
fi

echo "[3/4] API 已就绪，启动 Worker"
python -m ai_work_os.worker &
worker_pid=$!

echo "[4/4] 启动 Master Agent"
python -m ai_work_os.master_agent &
master_pid=$!

echo "AI Work OS 已启动。按 Ctrl+C 可停止三个进程。"
echo "聊天界面：http://${URL_HOST}:${API_PORT}/"
echo "任务控制台：http://${URL_HOST}:${API_PORT}/dashboard"

# If any service exits, stop the remaining processes instead of leaving a partial stack.
set +e
wait -n "$api_pid" "$worker_pid" "$master_pid"
exit_code=$?
set -e
echo "检测到服务进程退出（状态码：$exit_code），正在停止其余进程。" >&2
exit "$exit_code"
