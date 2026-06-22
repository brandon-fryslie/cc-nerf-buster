#!/usr/bin/env bash
# [LAW:single-enforcer] Reuse the legacy probe's proxy lifecycle so fresh
# measurement does not create a second proxy startup policy.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BINARY="${REPO_ROOT}/cc-nerf-buster"

if [[ ! -x "${BINARY}" ]]; then
  echo "[fresh-quota] building cc-nerf-buster" >&2
  (cd "${REPO_ROOT}" && go build -o cc-nerf-buster .)
fi

pick_free_port() {
  python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
}

PROXY_PORT="$(pick_free_port)"
METRICS_PORT="$(pick_free_port)"
while [[ "${METRICS_PORT}" == "${PROXY_PORT}" ]]; do
  METRICS_PORT="$(pick_free_port)"
done

DATA_DIR="${DATA_DIR:-${HOME}/.local/cc-nerf-buster}"
RUN_TS="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${RUN_DIR:-${DATA_DIR}/quota-runs/${RUN_TS}}"
mkdir -p "${RUN_DIR}"

PROXY_LOG="${RUN_DIR}/proxy.log"
USAGE_LOG="${RUN_DIR}/usage.jsonl"
DEBUG_LOG="${RUN_DIR}/debug.jsonl"
HAR_LOG="${RUN_DIR}/traffic.har"

echo "[fresh-quota] starting proxy on :${PROXY_PORT} (metrics :${METRICS_PORT}) — log: ${PROXY_LOG}" >&2
echo "[fresh-quota] run dir: ${RUN_DIR}" >&2

set -m
"${BINARY}" --port="${PROXY_PORT}" --metrics="${METRICS_PORT}" \
            --usage-log="${USAGE_LOG}" --debug-log="${DEBUG_LOG}" \
            --har-log="${HAR_LOG}" \
            >"${PROXY_LOG}" 2>&1 &
PROXY_PID=$!
set +m

cleanup() {
  if kill -0 "${PROXY_PID}" 2>/dev/null; then
    echo "[fresh-quota] stopping proxy (pid ${PROXY_PID})" >&2
    kill "${PROXY_PID}" 2>/dev/null || true
    wait "${PROXY_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

METRICS_URL="http://localhost:${METRICS_PORT}/metrics"
PROXY_URL="http://localhost:${PROXY_PORT}"

for _ in $(seq 1 50); do
  if ! kill -0 "${PROXY_PID}" 2>/dev/null; then
    echo "[fresh-quota] ERROR: proxy exited during startup; log:" >&2
    cat "${PROXY_LOG}" >&2
    exit 1
  fi
  if curl -sSf -o /dev/null "${METRICS_URL}" 2>/dev/null; then
    break
  fi
  sleep 0.1
done

if ! curl -sSf -o /dev/null "${METRICS_URL}"; then
  echo "[fresh-quota] ERROR: metrics endpoint ${METRICS_URL} never came up; log:" >&2
  cat "${PROXY_LOG}" >&2
  exit 1
fi

export PROXY_URL METRICS_URL

forward_to_driver() {
  if [[ -n "${DRIVER_PID:-}" ]] && kill -0 "${DRIVER_PID}" 2>/dev/null; then
    kill -INT "${DRIVER_PID}" 2>/dev/null || true
  fi
}
trap forward_to_driver INT TERM

CA_CERT="${DATA_DIR}/ca.crt"
if [[ ! -f "${CA_CERT}" ]]; then
  echo "[fresh-quota] ERROR: missing CA cert at ${CA_CERT}; run 'just install' first" >&2
  exit 1
fi
export CCNB_CA_CERT="${CA_CERT}"

MODEL="${MODEL:-claude-opus-4-7}"
echo "[fresh-quota] priming proxy with one claude call so baseline reflects real Anthropic state" >&2
if ! https_proxy="${PROXY_URL}" HTTPS_PROXY="${PROXY_URL}" \
     http_proxy="${PROXY_URL}" HTTP_PROXY="${PROXY_URL}" \
     NODE_EXTRA_CA_CERTS="${CA_CERT}" SSL_CERT_FILE="${CA_CERT}" \
     CURL_CA_BUNDLE="${CA_CERT}" REQUESTS_CA_BUNDLE="${CA_CERT}" \
     CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 DISABLE_PROMPT_CACHING=1 \
     claude -p --model "${MODEL}" --system-prompt '' \
            --no-session-persistence --tools '' \
            -- "Reply with the single word: ok" \
     >"${PROXY_LOG}.prime" 2>&1; then
  echo "[fresh-quota] ERROR: priming claude call failed; output:" >&2
  cat "${PROXY_LOG}.prime" >&2
  exit 1
fi
echo "[fresh-quota] priming complete" >&2

python3 "${SCRIPT_DIR}/measure.py" drive --run-dir="${RUN_DIR}" "$@" &
DRIVER_PID=$!
DRIVER_EXIT=0
wait "${DRIVER_PID}" || DRIVER_EXIT=$?
exit "${DRIVER_EXIT}"
