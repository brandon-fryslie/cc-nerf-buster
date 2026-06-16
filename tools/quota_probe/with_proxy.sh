#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BINARY="${REPO_ROOT}/cc-nerf-buster"

pick_free_port() {
  python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

if [[ ! -x "${BINARY}" ]]; then
  echo "[fresh-quota] building cc-nerf-buster" >&2
  (cd "${REPO_ROOT}" && go build -o cc-nerf-buster .)
fi

DATA_DIR="${DATA_DIR:-${HOME}/.local/cc-nerf-buster}"
RUN_TS="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${RUN_DIR:-${DATA_DIR}/quota-runs/${RUN_TS}}"
mkdir -p "${RUN_DIR}"

PROXY_PORT="$(pick_free_port)"
METRICS_PORT="$(pick_free_port)"
while [[ "${METRICS_PORT}" == "${PROXY_PORT}" ]]; do
  METRICS_PORT="$(pick_free_port)"
done

PROXY_LOG="${RUN_DIR}/proxy.log"
USAGE_LOG="${RUN_DIR}/usage.jsonl"
DEBUG_LOG="${RUN_DIR}/debug.jsonl"
HAR_LOG="${RUN_DIR}/traffic.har"

echo "[fresh-quota] run dir: ${RUN_DIR}" >&2
echo "[fresh-quota] proxy :${PROXY_PORT}; metrics :${METRICS_PORT}" >&2

"${BINARY}" --port="${PROXY_PORT}" --metrics="${METRICS_PORT}" \
            --usage-log="${USAGE_LOG}" --debug-log="${DEBUG_LOG}" \
            --har-log="${HAR_LOG}" \
            >"${PROXY_LOG}" 2>&1 &
PROXY_PID=$!

cleanup() {
  if kill -0 "${PROXY_PID}"; then
    echo "[fresh-quota] stopping proxy ${PROXY_PID}" >&2
    kill "${PROXY_PID}"
    if wait "${PROXY_PID}"; then
      echo "[fresh-quota] proxy stopped cleanly" >&2
    else
      echo "[fresh-quota] proxy stopped after shutdown signal" >&2
    fi
  fi
}
trap cleanup EXIT

METRICS_URL="http://localhost:${METRICS_PORT}/metrics"
PROXY_URL="http://localhost:${PROXY_PORT}"
export METRICS_URL PROXY_URL

CA_CERT="${DATA_DIR}/ca.crt"
if [[ -f "${CA_CERT}" ]]; then
  export CCNB_CA_CERT="${CA_CERT}"
fi

for _ in $(seq 1 50); do
  if curl -fsS -o /dev/null "${METRICS_URL}"; then
    break
  fi
  sleep 0.1
done

curl -fsS -o /dev/null "${METRICS_URL}"

python3 "${SCRIPT_DIR}/measure.py" drive --run-dir "${RUN_DIR}" "$@"
