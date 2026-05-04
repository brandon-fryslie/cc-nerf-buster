# Capacity Probe Report

- Run directory: `/Users/bmf/.local/cc-nerf-buster/probe-runs/20260427T193240Z`
- Started:       `20260427T193240Z` UTC
- Model pinned:  `claude-opus-4-7`
- Metrics URL:   `http://localhost:57459/metrics`
- Iterations:    76
- Window:        `5h`
- Target 5h ticks: 3 (needs 4 crossings)
- Target 7d ticks: 1 (needs 2 crossings)
- Observed 5h crossings: 4 → 3 clean measured tick(s)
- Observed 7d crossings: 0 → 0 clean measured tick(s)

## Baseline → Final

| Field   | Baseline        | Final           | Δ              |
|---------|-----------------|-----------------|----------------|
| util_5h | 0.04        | 0.08        | +0.0400       |
| util_7d | 0.01        | 0.01        | +0.0000       |
| cost    | 0.084873 | 9.610602 | +9.525730 |

## Capacity Estimates

Internal unit: weighted-price-dollar-equivalent. Three estimates are compared:

- **Midpoint** — recommended. For each tick boundary K, estimate the true cost at K as `(c_pre + c_post) / 2`. Per-tick capacity = `(mid_K+1 − mid_K) / 0.01`. Removes the systematic bias that pre/post alone have.
- **Post-post** — uses the first snapshot after each boundary crossing. Overshoots (bias: high) but partially cancels if call sizes are uniform.
- **Pre-pre** — uses the last snapshot before each boundary crossing. Undershoots (bias: low).

Proxy lifetime estimate (exposed at `/metrics`, accumulated across all observations since the proxy started — includes data from before this probe run):

- 5h capacity (proxy): `187.845557` (weighted-USD)
- 7d capacity (proxy): `595.873917` (weighted-USD)

### Probe-derived capacity (this run only)

| Window | Method     | Capacity (weighted-USD) | # ticks |
|--------|------------|-------------------------|---------|
| 5h | midpoint   |              275.311667 |       3 |
| 5h | post-post  |              273.726333 |       3 |
| 5h | pre-pre    |              276.897000 |       3 |
| 7d | midpoint   |                       — |       0 |
| 7d | post-post  |                       — |       0 |
| 7d | pre-pre    |                       — |       0 |

### Bounds

Low = pre-pre, midpoint = recommended, high = post-post.

#### 5h

| Bound | Weighted-USD | Input tokens (full quota) | Input tokens / 1% tick |
|-------|--------------|---------------------------|------------------------|
| Low   | 276.897000 | 55,379,400 | 553,794 |
| Mid   | 275.311667 | 55,062,333 | 550,623 |
| High  | 273.726333 | 54,745,267 | 547,453 |

#### 7d

_(no clean measured ticks in this run — only the proxy lifetime estimate is available)_

### Capacity as tokens (midpoint estimate, this run)

#### 5h

| Model   | Input tokens        | Output tokens       |
|---------|---------------------|---------------------|
| haiku   |         275,311,667 |          55,062,333 |
| sonnet  |          91,770,556 |          18,354,111 |
| opus    |          55,062,333 |          11,012,467 |

#### 7d

_(no clean measured ticks in this run — no probe-derived number available. The proxy lifetime estimate above is still valid.)_

## Measured Ticks (per-tick breakdown)

One row per clean measured tick. A clean tick is bracketed by two consecutive boundary crossings with no multi-tick jumps. Compare the three capacity columns to see the spread — they should cluster tightly if the probe is well-calibrated.

### 5h

| # | tick        | cap_midpoint | cap_post | cap_pre |
|---|-------------|--------------|----------|---------|
| 1 | 0.05 → 0.06 | 272.6973     | 267.9413 | 277.4533 |
| 2 | 0.06 → 0.07 | 288.1268     | 288.1268 | 288.1268 |
| 3 | 0.07 → 0.08 | 265.1110     | 265.1110 | 265.1110 |

### 7d

_(no clean measured ticks in this run)_

## Provenance / audit trail

- `manifest.json`        — run config, baseline snapshot
- `snapshots.jsonl`      — every `/metrics` scrape (parsed JSON)
- `raw-metrics/`         — verbatim Prometheus exposition bodies
- `iterations.jsonl`     — every `claude -p` invocation (prompt, exit, wall time)
- `prompts/`             — exact prompt text sent per iteration
- `claude-output/`       — literal stdout+stderr per iteration
- `crossings.jsonl`      — every detected tick-boundary crossing (derived)
- `measured_ticks.jsonl` — clean measured ticks with all three capacity estimates (derived)
- `bounds.json`          — machine-readable low/mid/high bounds (derived)
- `probe.sh`             — thin shell wrapper as-run
- `probe.py`             — the Python probe driver as-run
- `report.py`            — this script as-run
- `scripts.sha256`       — SHA-256 of driver and report script

The canonical source is `snapshots.jsonl` + `raw-metrics/`. Everything else is reproducible by re-running `report.py <run_dir>`.
