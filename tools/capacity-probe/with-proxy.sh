#!/usr/bin/env bash
# Start cc-nerf-buster on random free ports, run the probe against it,
# then stop the proxy when the probe exits (success, failure, or signal).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BINARY="${REPO_ROOT}/cc-nerf-buster"

if [[ ! -x "${BINARY}" ]]; then
  echo "[with-proxy] building cc-nerf-buster" >&2
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

LOG_DIR="${TMPDIR:-/tmp}"
PROXY_LOG="$(mktemp "${LOG_DIR%/}/cc-nerf-buster-probe.XXXXXX")"

echo "[with-proxy] starting proxy on :${PROXY_PORT} (metrics :${METRICS_PORT}) — log: ${PROXY_LOG}" >&2
# Enable job control so the backgrounded proxy gets its own process group;
# without this, Ctrl-C in the foreground (probe) is delivered to the proxy
# too, killing it mid-iteration before the probe can finish gracefully.
set -m
"${BINARY}" --port="${PROXY_PORT}" --metrics="${METRICS_PORT}" >"${PROXY_LOG}" 2>&1 &
PROXY_PID=$!
set +m

cleanup() {
  if kill -0 "${PROXY_PID}" 2>/dev/null; then
    echo "[with-proxy] stopping proxy (pid ${PROXY_PID})" >&2
    kill "${PROXY_PID}" 2>/dev/null || true
    wait "${PROXY_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

METRICS_URL="http://localhost:${METRICS_PORT}/metrics"
PROXY_URL="http://localhost:${PROXY_PORT}"

# Wait for /metrics to come up. Fail fast if the proxy died on startup.
for _ in $(seq 1 50); do
  if ! kill -0 "${PROXY_PID}" 2>/dev/null; then
    echo "[with-proxy] ERROR: proxy exited during startup; log:" >&2
    cat "${PROXY_LOG}" >&2
    exit 1
  fi
  if curl -sSf -o /dev/null "${METRICS_URL}" 2>/dev/null; then
    break
  fi
  sleep 0.1
done

if ! curl -sSf -o /dev/null "${METRICS_URL}"; then
  echo "[with-proxy] ERROR: metrics endpoint ${METRICS_URL} never came up; log:" >&2
  cat "${PROXY_LOG}" >&2
  exit 1
fi

export PROXY_URL METRICS_URL

# Forward INT/TERM to the probe but keep bash alive so the EXIT trap can
# stop the proxy. Without this, Ctrl-C would deliver the signal to bash
# only; bash would die before the trap ran and the proxy would leak.
forward_to_probe() {
  if [[ -n "${PROBE_PID:-}" ]] && kill -0 "${PROBE_PID}" 2>/dev/null; then
    kill -INT "${PROBE_PID}" 2>/dev/null || true
  fi
}
trap forward_to_probe INT TERM

# Prime the proxy with one real request so it has a true baseline from Anthropic
# response headers. A fresh proxy reports util=0 not because util IS zero, but
# because it has no data yet — measuring deltas against that "zero" attributes
# all of the user's pre-existing quota usage to the first iteration.
# Dry-run can't prime (no real API call), so its baseline is necessarily
# degenerate — that's an inherent limitation of dry-run on an ephemeral proxy.
DRY_RUN=0
for arg in "$@"; do
  if [[ "$arg" == "--dry-run" ]]; then DRY_RUN=1; fi
done

if [[ "$DRY_RUN" -eq 0 ]]; then
  DATA_DIR="${DATA_DIR:-${HOME}/.local/cc-nerf-buster}"
  CA_CERT="${DATA_DIR}/ca.crt"
  if [[ ! -f "${CA_CERT}" ]]; then
    echo "[with-proxy] ERROR: missing CA cert at ${CA_CERT}; run 'just install' first" >&2
    exit 1
  fi
  MODEL="${MODEL:-claude-opus-4-7}"
  echo "[with-proxy] priming proxy with one claude call so baseline reflects real Anthropic state" >&2
  if ! https_proxy="${PROXY_URL}" HTTPS_PROXY="${PROXY_URL}" \
       http_proxy="${PROXY_URL}" HTTP_PROXY="${PROXY_URL}" \
       NODE_EXTRA_CA_CERTS="${CA_CERT}" SSL_CERT_FILE="${CA_CERT}" \
       CURL_CA_BUNDLE="${CA_CERT}" REQUESTS_CA_BUNDLE="${CA_CERT}" \
       claude -p --model "${MODEL}" --system-prompt '' \
              --no-session-persistence --tools '' \
              -- "Reply with the single word: ok" \
       >"${PROXY_LOG}.prime" 2>&1; then
    echo "[with-proxy] ERROR: priming claude call failed; output:" >&2
    cat "${PROXY_LOG}.prime" >&2
    exit 1
  fi
  echo "[with-proxy] priming complete" >&2
fi

uv run --with rich python "${SCRIPT_DIR}/probe.py" "$@" &
PROBE_PID=$!
PROBE_EXIT=0
wait "${PROBE_PID}" || PROBE_EXIT=$?
exit "${PROBE_EXIT}"
