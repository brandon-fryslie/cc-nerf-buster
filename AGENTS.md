# AGENTS.md for `cc-nerf-buster`

This file defines how agents should work in this repository. It is derived from the repo's existing Copilot instructions and the architectural laws supplied for this project.

## Purpose

`cc-nerf-buster` is a Go proxy that MITMs configured Anthropic API traffic, extracts usage and quota signals, logs canonical request events, and estimates 5-hour and 7-day quota capacity.

Primary success criteria:

- The proxy still builds with `just build`.
- Verification passes with `just test` and `just vet` unless the task is documentation-only.
- The proxy/metrics dataflow remains intact: intercept -> extract -> log -> record metrics -> persist estimates.
- Quota estimation remains reproducible from canonical runtime artifacts under `data-dir`.

## Source Documents

- `.github/copilot-instructions.md` is the repo-specific implementation map and command reference.
- This `AGENTS.md` is the agent-facing operating contract for the repo.

## Non-Negotiable Laws

These apply to every change. Cite them in code comments whenever a law materially drives a decision:

- `// [LAW:dataflow-not-control-flow] reason`
- `// [LAW:one-source-of-truth] reason`
- `// [LAW:single-enforcer] reason`
- `// [LAW:one-way-deps] reason`
- `// [LAW:one-type-per-behavior] reason`
- `// [LAW:verifiable-goals] reason`

If a law must be violated, mark it explicitly:

- `// [LAW:<token>] exception: reason`

Definitions:

- `dataflow-not-control-flow`: Keep the operation sequence stable. Variability belongs in values, not in whether stages run.
- `one-source-of-truth`: Every concept has exactly one canonical representation. Derive everything else from it.
- `single-enforcer`: Cross-cutting invariants are enforced at one boundary only.
- `one-way-deps`: Dependencies point in one direction. Do not introduce cycles or upward calls.
- `one-type-per-behavior`: Do not create parallel types when configuration or data instances are sufficient.
- `verifiable-goals`: Every task must end with deterministic verification when feasible.

Additional standing guidance:

- Prefer deleting compatibility layers over adding shims.
- Keep modules split by reason-to-change.
- Put invariants at boundaries, not scattered through internals.
- Tests must assert behavior, not implementation structure.
- No silent fallbacks.

## Repo Architecture

### Canonical Flow

The core path is:

1. `main.go` parses flags, initializes runtime state, and starts the proxy and metrics servers.
2. `proxy.go` handles CONNECT tunneling/MITM interception and forwards traffic upstream.
3. `anthropic.go` extracts request, response, SSE usage, pricing, and quota metadata.
4. `log.go` writes canonical JSONL events.
5. `metrics.go` records counters/gauges and persists quota estimation state.

Keep this order legible. If you change behavior, do it by changing the data passed between stages rather than adding ad hoc branching deep in the pipeline. `// [LAW:dataflow-not-control-flow] proxy processing should remain a stable staged pipeline`

### Canonical Data Shapes

- `APIEvent` is the canonical log/event record. Do not create alternate event schemas unless there is a hard boundary that requires translation. `// [LAW:one-source-of-truth] APIEvent is the authoritative usage/quota record`
- Optional event fields use pointers. Preserve that contract instead of inventing parallel sentinel values.
- Error values should remain short machine-readable codes, not freeform prose.
- Persistent quota estimates live in `quota_estimates.json` under `data-dir`. Runtime usage logs live in `usage.jsonl`.

### Boundary Ownership

- Host filtering is enforced by configured `--upstream-url` values. Do not duplicate capture gating in scattered call sites. `// [LAW:single-enforcer] upstream interception should be decided at the proxy boundary`
- Metrics persistence is owned by `metrics.go`.
- CA creation/loading is owned by `ssl_inspect.go`.
- Throttled operational logging should go through `throttledLog(...)`.

### Cross-File Consistency

- Model pricing has two required representations today:
  - `anthropic.go`: `modelPricing`
  - `tools/capacity-probe/report.py`: `PRICING`
- If pricing changes, update both in the same change and explain the synchronization point in code/comments. `// [LAW:one-source-of-truth] pricing inputs must remain explicitly synchronized across runtime and reporting`

## Working Rules

### Build, Test, and Format

Preferred commands:

- `just build`
- `just test`
- `just vet`
- `just fmt`
- `just check`

Direct equivalents:

- `go build -o cc-nerf-buster .`
- `go test ./...`
- `go vet ./...`
- `gofmt -w .`

Single-test examples:

- `go test ./... -run '^TestName$'`
- `go test . -run '^TestName$'`

### Change Discipline

- Make the smallest change that preserves the staged proxy/logging/metrics architecture.
- Do not add knobs casually. New flags or modes require a concrete use case, an owner, and an exit plan.
- Avoid introducing new global mutable state.
- Prefer explicit translation layers at boundaries over leaking legacy/new formats across modules.
- Keep CLI behavior intentional: exit codes and stdout/stderr semantics are part of the contract.

### Repo-Specific Constraints

- Only configured upstream hosts are inspected; other traffic remains pass-through.
- SSE and non-streaming responses intentionally take different extraction paths. Do not collapse them unless behavior remains equivalent.
- Non-streaming usage extraction currently buffers up to 10 MB. If you change this limit or behavior, update the documentation and tests with the same change.
- Unknown models are not a soft success path; they increment dedicated error counters and should remain visible in probe/report workflows.
- Internal metric keys may use packed separators, while persisted estimate keys use `org/upstream`. Do not blur those representations without a clear boundary adapter. `// [LAW:single-enforcer] internal and persisted key formats should translate in one place`

## Verification Expectations

Default verification for code changes:

1. Run `just fmt`.
2. Run `just vet`.
3. Run `just test`.
4. Run `just build`.

For docs-only changes, verify the referenced paths and commands still exist.

For metrics/quota work, also verify at least one of:

- estimator persistence/load behavior
- metrics exposition for the changed counters/gauges
- capacity-probe compatibility if pricing or report inputs changed

Do not hand testing back to the user unless there is no deterministic way to verify locally. `// [LAW:verifiable-goals] agent work is not complete without concrete verification when verification is possible`

## File Map

- `main.go`: entrypoint, flag parsing, lifecycle, server wiring
- `proxy.go`: HTTP proxy behavior, CONNECT handling, MITM path
- `anthropic.go`: request/response parsing, SSE usage parsing, pricing/cost logic
- `metrics.go`: counters, gauges, quota estimators, persistence
- `log.go`: JSONL event sink
- `ssl_inspect.go`: CA generation/loading and TLS inspection support
- `throttle.go`: throttled operational logging
- `tools/capacity-probe/`: external calibration and reporting workflow

## When Unsure

- Follow the canonical data path.
- Reuse existing types and boundaries.
- Put variability in data, not control flow.
- Add or update verification before declaring the task complete.
