# Copilot Instructions for `cc-nerf-buster`

## Build, test, and lint commands

- Preferred task runner:
  - `just build` (builds `cc-nerf-buster`)
  - `just test` (runs `go test ./...`)
  - `just vet` (runs `go vet ./...`)
  - `just fmt` (runs `gofmt -w .`)
  - `just check` (runs `fmt`, `vet`, then `build`)
- Direct Go equivalents:
  - `go build -o cc-nerf-buster .`
  - `go test ./...`
  - `go vet ./...`
  - `gofmt -w .`
- Run a single test by name:
  - `go test ./... -run '^TestName$'`
  - For one package only (repo root package): `go test . -run '^TestName$'`

## High-level architecture

- `main.go` wires the app together:
  - Parses flags (`--port`, `--metrics`, `--data-dir`, repeatable `--upstream-url`, optional `--proxy`).
  - Initializes persistent data dir, CA (`ssl_inspect.go`), metrics (`metrics.go`), JSONL logging (`log.go`), and the proxy handler (`proxy.go`).
  - Runs two HTTP servers: proxy traffic and Prometheus metrics.
- `proxy.go` is the core traffic path:
  - CONNECT requests to configured upstream hosts are MITM-inspected via dynamic certs from `CertAuthority`.
  - Other CONNECT traffic is tunneled blindly (or chained through downstream proxy if configured).
  - Captured HTTPS requests are replayed upstream, response bodies are streamed to the client, and usage/quota data is extracted for logging/metrics.
- `anthropic.go` defines extraction and cost logic:
  - Request/response parsing (`model`, `usage`, quota headers, metadata headers).
  - SSE usage parsing for streaming responses (`message_start`/`message_delta` events).
  - Weighted pricing table and `RequestCost` used by metrics.
- `metrics.go` maintains in-memory counters/gauges and exposes `/metrics`:
  - Records per-request counters and quota utilization gauges.
  - Maintains cumulative weighted cost and rolling 5h/7d capacity estimates.
  - Persists estimator state to `quota_estimates.json` in `data-dir`.
- `tools/capacity-probe` is an external calibration workflow:
  - `probe.sh` drives repeated `claude -p` calls and snapshots metrics.
  - `report.py` produces reproducible run reports from probe artifacts.

## Key conventions in this codebase

- Capture scope is host-gated: only hosts in `--upstream-url` (default `api.anthropic.com`) are inspected; everything else is pass-through proxy behavior.
- `APIEvent` is the canonical log record. Optional fields use pointers; errors are short machine-readable codes (for example `quota_headers_missing`, `model_field_missing`) rather than freeform text.
- Streaming and non-streaming responses are handled differently on purpose:
  - SSE responses are forwarded incrementally while parsing usage from event lines.
  - Non-streaming responses are buffered up to 10MB for usage extraction; larger payloads are still forwarded but usage extraction is skipped with an error code.
- Model pricing must stay synchronized across files:
  - `anthropic.go` (`modelPricing`) and `tools/capacity-probe/report.py` (`PRICING`) need matching ratios/values.
  - Unknown models intentionally increment `ccnb_no_model_error_*` counters and are treated as a probe-failure condition.
- Metrics keying uses packed internal keys (`\x00` separators) for map indexes, while persisted estimate keys use `org/upstream` strings.
- Throttled operational logging should go through `throttledLog(category, ...)` to avoid noisy repeated logs (60-second suppression window per category).
- Persistent runtime artifacts live under `data-dir` (`usage.jsonl`, `quota_estimates.json`, `ca.crt`, `ca.key`) and are part of normal operation, not temporary files.
