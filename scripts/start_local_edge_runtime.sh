#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/start_local_edge_runtime.sh /path/to/device-graph.json

Starts the loopback-only local bridge and Uni-Lab-OS as one supervised runtime.
The graph must be selected explicitly, either as the first argument or through
UNILAB_GRAPH.

Environment:
  UNILAB_PYTHON           Python executable used for local_bridge (default: python3)
  UNILAB_COMMAND          Uni-Lab-OS executable (default: unilab)
  UNILAB_OS_PORT          OS internal HTTP port (default: 8002)
  UNILAB_API_PORT         frontend unified API port (default: 8014)
  UNILAB_SCHEDULE_PORT    OS schedule WebSocket port (default: 8890)
  UNILAB_READY_TIMEOUT    action-catalog readiness timeout in seconds (default: 90)
  UNILAB_CONFIG           optional Uni-Lab-OS config.py
  UNILAB_WORKING_DIR      optional Uni-Lab-OS working directory
  UNILAB_BACKEND          ros, simple, or automancer (default: ros)
  UNILAB_TEST_MODE        set to 1 to enable --test_mode
  UNILAB_SKIP_ENV_CHECK   set to 1 to enable --skip_env_check
EOF
}

fail() {
  echo "[edge-runtime] $*" >&2
  exit 1
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi
if (( $# > 1 )); then
  usage >&2
  fail "only one graph path may be supplied"
fi

graph_path="${1:-${UNILAB_GRAPH:-}}"
unilab_python="${UNILAB_PYTHON:-python3}"
unilab_command="${UNILAB_COMMAND:-unilab}"
os_port="${UNILAB_OS_PORT:-8002}"
api_port="${UNILAB_API_PORT:-8014}"
schedule_port="${UNILAB_SCHEDULE_PORT:-8890}"
ready_timeout="${UNILAB_READY_TIMEOUT:-90}"
config_path="${UNILAB_CONFIG:-}"
working_dir="${UNILAB_WORKING_DIR:-}"
backend="${UNILAB_BACKEND:-ros}"

[[ -n "${graph_path}" ]] || fail "select a device graph with an argument or UNILAB_GRAPH"
[[ -f "${graph_path}" ]] || fail "device graph does not exist: ${graph_path}"
if [[ -n "${config_path}" && ! -f "${config_path}" ]]; then
  fail "config file does not exist: ${config_path}"
fi
if [[ ! "${ready_timeout}" =~ ^[1-9][0-9]*$ ]]; then
  fail "UNILAB_READY_TIMEOUT must be a positive integer"
fi
for port_value in "${os_port}" "${api_port}" "${schedule_port}"; do
  if [[ ! "${port_value}" =~ ^[0-9]+$ ]] || (( port_value < 1 || port_value > 65535 )); then
    fail "invalid TCP port: ${port_value}"
  fi
done
case "${backend}" in
  ros|simple|automancer) ;;
  *) fail "UNILAB_BACKEND must be ros, simple, or automancer" ;;
esac

if [[ "${unilab_python}" == */* ]]; then
  [[ -x "${unilab_python}" ]] || fail "Python executable is not executable: ${unilab_python}"
else
  command -v "${unilab_python}" >/dev/null || fail "Python executable was not found: ${unilab_python}"
fi
if [[ "${unilab_command}" == */* ]]; then
  [[ -x "${unilab_command}" ]] || fail "Uni-Lab executable is not executable: ${unilab_command}"
else
  command -v "${unilab_command}" >/dev/null || fail "Uni-Lab executable was not found: ${unilab_command}"
fi

bridge_pid=""
os_pid=""

cleanup() {
  local pid
  for pid in "${os_pid}" "${bridge_pid}"; do
    if [[ -n "${pid}" ]]; then
      kill "${pid}" 2>/dev/null || true
    fi
  done
  for pid in "${os_pid}" "${bridge_pid}"; do
    if [[ -n "${pid}" ]]; then
      wait "${pid}" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT INT TERM

http_json_matches() {
  local url="$1"
  local expression="$2"
  "${unilab_python}" - "${url}" "${expression}" <<'PY'
import json
import sys
import urllib.request

url, expression = sys.argv[1:]
try:
    with urllib.request.urlopen(url, timeout=1.0) as response:
        payload = json.load(response)
except Exception:
    raise SystemExit(1)

if expression == "health":
    ready = payload == {"status": "ok"}
elif expression == "catalog":
    ready = payload.get("available") is True and isinstance(
        payload.get("actions"), list
    )
else:
    ready = False
raise SystemExit(0 if ready else 1)
PY
}

wait_until() {
  local label="$1"
  local url="$2"
  local expression="$3"
  local deadline=$((SECONDS + ready_timeout))

  until http_json_matches "${url}" "${expression}"; do
    if [[ -n "${bridge_pid}" ]] && ! kill -0 "${bridge_pid}" 2>/dev/null; then
      fail "local bridge exited before ${label} became ready"
    fi
    if [[ -n "${os_pid}" ]] && ! kill -0 "${os_pid}" 2>/dev/null; then
      fail "Uni-Lab-OS exited before ${label} became ready"
    fi
    if (( SECONDS >= deadline )); then
      fail "timed out waiting for ${label}: ${url}"
    fi
    sleep 0.25
  done
}

bridge_args=(
  -m unilabos.app.local_bridge.server
  --host 127.0.0.1
  --schedule-port "${schedule_port}"
  --api-port "${api_port}"
  --execution-http-url "http://127.0.0.1:${os_port}"
)

echo "[edge-runtime] starting local bridge on 127.0.0.1:${api_port}"
"${unilab_python}" "${bridge_args[@]}" &
bridge_pid=$!
wait_until \
  "local bridge" \
  "http://127.0.0.1:${api_port}/health" \
  "health"

os_args=(
  --graph "${graph_path}"
  --backend "${backend}"
  --app_bridges websocket fastapi
  --port "${os_port}"
  --schedule_addr "ws://127.0.0.1:${schedule_port}/api/v1/ws/schedule"
  --disable_browser
)
if [[ -n "${config_path}" ]]; then
  os_args+=(--config "${config_path}")
fi
if [[ -n "${working_dir}" ]]; then
  mkdir -p "${working_dir}"
  os_args+=(--working_dir "${working_dir}")
fi
if [[ "${UNILAB_TEST_MODE:-0}" == "1" ]]; then
  os_args+=(--test_mode)
fi
if [[ "${UNILAB_SKIP_ENV_CHECK:-0}" == "1" ]]; then
  os_args+=(--skip_env_check)
fi

echo "[edge-runtime] starting Uni-Lab-OS with graph ${graph_path}"
"${unilab_command}" "${os_args[@]}" &
os_pid=$!

catalog_url="http://127.0.0.1:${api_port}/api/runtime/local/actions"
wait_until "Runtime Action Catalog" "${catalog_url}" "catalog"
echo "[edge-runtime] ready: ${catalog_url}"

wait -n "${bridge_pid}" "${os_pid}"
