# Capacity Probe Report

- Run directory: `/Users/bmf/.local/cc-nerf-buster/probe-runs/20260503T205745Z`
- Started:       `20260503T205745Z` UTC
- Model pinned:  `claude-opus-4-7`
- Metrics URL:   `http://localhost:58373/metrics`
- Iterations:    140
- Window:        `both`
- Target 5h ticks: 3 (needs 4 crossings)
- Target 7d ticks: 1 (needs 2 crossings)
- Observed 5h crossings: 9 → 8 clean measured tick(s)
- Observed 7d crossings: 2 → 1 clean measured tick(s)

## Baseline → Final

| Field   | Baseline        | Final           | Δ              |
|---------|-----------------|-----------------|----------------|
| util_5h | 0.01        | 0.1        | +0.0900       |
| util_7d | 0.98        | 1.0        | +0.0200       |
| cost    | 0.144930 | 25.042965 | +24.898035 |

## Capacity Estimates

Internal unit: weighted-price-dollar-equivalent. Three estimates are compared:

- **Midpoint** — recommended. For each tick boundary K, estimate the true cost at K as `(c_pre + c_post) / 2`. Per-tick capacity = `(mid_K+1 − mid_K) / 0.01`. Removes the systematic bias that pre/post alone have.
- **Post-post** — uses the first snapshot after each boundary crossing. Overshoots (bias: high) but partially cancels if call sizes are uniform.
- **Pre-pre** — uses the last snapshot before each boundary crossing. Undershoots (bias: low).

Proxy lifetime estimate (exposed at `/metrics`, accumulated across all observations since the proxy started — includes data from before this probe run):

- 5h capacity (proxy): `222.590080` (weighted-USD)
- 7d capacity (proxy): `651.243464` (weighted-USD)

### Probe-derived capacity (this run only)

| Window | Method     | Capacity (weighted-USD) | # ticks |
|--------|------------|-------------------------|---------|
| 5h | midpoint   |              273.308750 |       8 |
| 5h | post-post  |              272.845063 |       8 |
| 5h | pre-pre    |              273.772438 |       8 |
| 7d | midpoint   |             1576.503500 |       1 |
| 7d | post-post  |             1572.794000 |       1 |
| 7d | pre-pre    |             1580.213000 |       1 |

### Bounds

Low = pre-pre, midpoint = recommended, high = post-post.

#### 5h

| Bound | Weighted-USD | Input tokens (full quota) | Input tokens / 1% tick |
|-------|--------------|---------------------------|------------------------|
| Low   | 273.772438 | 54,754,488 | 547,545 |
| Mid   | 273.308750 | 54,661,750 | 546,618 |
| High  | 272.845063 | 54,569,013 | 545,690 |

#### 7d

| Bound | Weighted-USD | Input tokens (full quota) | Input tokens / 1% tick |
|-------|--------------|---------------------------|------------------------|
| Low   | 1580.213000 | 316,042,600 | 3,160,426 |
| Mid   | 1576.503500 | 315,300,700 | 3,153,007 |
| High  | 1572.794000 | 314,558,800 | 3,145,588 |

### Capacity as tokens (midpoint estimate, this run)

#### 5h

| Model   | Input tokens        | Output tokens       |
|---------|---------------------|---------------------|
| haiku   |         273,308,750 |          54,661,750 |
| sonnet  |          91,102,917 |          18,220,583 |
| opus    |          54,661,750 |          10,932,350 |

#### 7d

| Model   | Input tokens        | Output tokens       |
|---------|---------------------|---------------------|
| haiku   |       1,576,503,500 |         315,300,700 |
| sonnet  |         525,501,167 |         105,100,233 |
| opus    |         315,300,700 |          63,060,140 |

## Measured Ticks (per-tick breakdown)

One row per clean measured tick. A clean tick is bracketed by two consecutive boundary crossings with no multi-tick jumps. Compare the three capacity columns to see the spread — they should cluster tightly if the probe is well-calibrated.

### 5h

| # | tick        | cap_midpoint | cap_post | cap_pre |
|---|-------------|--------------|----------|---------|
| 1 | 0.02 → 0.03 | 180.6920     | 177.4605 | 183.9235 |
| 2 | 0.03 → 0.04 | 249.0605     | 248.5825 | 249.5385 |
| 3 | 0.04 → 0.05 | 290.8298     | 290.8298 | 290.8298 |
| 4 | 0.05 → 0.06 | 439.8860     | 519.3440 | 360.4280 |
| 5 | 0.06 → 0.07 | 140.6015     | 64.8530 | 216.3500 |
| 6 | 0.07 → 0.08 | 298.4393     | 295.1608 | 301.7178 |
| 7 | 0.08 → 0.09 | 287.9565     | 287.5255 | 288.3875 |
| 8 | 0.09 → 0.10 | 299.0045     | 299.0045 | 299.0045 |

### 7d

| # | tick        | cap_midpoint | cap_post | cap_pre |
|---|-------------|--------------|----------|---------|
| 1 | 0.99 → 1.00 | 1576.5035     | 1572.7940 | 1580.2130 |

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
