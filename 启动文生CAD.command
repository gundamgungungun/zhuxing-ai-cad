#!/bin/zsh

set -u

PROJECT_DIR="/Users/xuzihan/Desktop/个人项目/文生模型"
PYTHON_BIN="/opt/homebrew/Caskroom/miniforge/base/envs/multi_agent_cad/bin/python"
APP_URL="http://127.0.0.1:8000"
HEALTH_URL="${APP_URL}/api/health"

if curl --silent --fail "${HEALTH_URL}" >/dev/null 2>&1; then
  open "${APP_URL}"
  exit 0
fi

cd "${PROJECT_DIR}" || exit 1

"${PYTHON_BIN}" -m multi_agent_cad.web &
SERVER_PID=$!

cleanup() {
  if kill -0 "${SERVER_PID}" >/dev/null 2>&1; then
    kill "${SERVER_PID}" >/dev/null 2>&1
  fi
}
trap cleanup EXIT INT TERM HUP

for _ in {1..40}; do
  if curl --silent --fail "${HEALTH_URL}" >/dev/null 2>&1; then
    open "${APP_URL}"
    echo ""
    echo "铸形已启动：${APP_URL}"
    echo "关闭这个终端窗口即可停止服务。"
    wait "${SERVER_PID}"
    exit $?
  fi

  if ! kill -0 "${SERVER_PID}" >/dev/null 2>&1; then
    echo "启动失败，请保留这个窗口中的报错信息。"
    wait "${SERVER_PID}"
    exit $?
  fi

  sleep 0.5
done

echo "服务启动超时，请保留这个窗口中的报错信息。"
exit 1
