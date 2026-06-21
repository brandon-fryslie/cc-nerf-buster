#!/usr/bin/env python3
"""
report.py — post-run analysis for capacity_probe.

Reads snapshots.jsonl + iterations.jsonl + manifest.json from a run directory
and produces:
  - deltas.jsonl   — one row per observed tick crossing (5h or 7d)
  - bounds.json    — machine-readable low/mid/high quota bounds
  - report.md      — human-readable summary with capacity expressed as tokens

Pure stdlib. No external deps. Deterministic — regenerating from the same
inputs produces identical output.

Internal capacity unit: weighted-price-dollar-equivalent. Formula:
  weighted_tokens_consumed × (price_ratio_scale / 1e6) → dollars
  capacity = Σ(Δcost) / Σ(Δutilization)

Token-count outputs are derived: tokens = capacity × 1e6 / price_per_MTok for
the requested model/direction. This mirrors how the proxy does the conversion
in metrics.go (writeTokenCapacity was removed; the report does it client-side).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Pricing in $/MTok. Mirrors modelPricing in anthropic.go. If this drifts from
# the proxy's table, token counts below will disagree with the proxy's view.
PRICING = {
    "haiku":  {"input": 1.00, "output": 5.00},
    "sonnet": {"input": 3.00, "output": 15.00},
    "opus":   {"input": 5.00, "output": 25.00},
}


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def detect_boundaries(snaps: list[dict], util_key: str) -> list[dict]:
    """
    Identify every tick-boundary crossing in a snapshot stream.

    A boundary crossing is a transition where the util bucket changes. Each
    crossing is bracketed by two observations:
      - pre:  the last snapshot BEFORE the bucket advanced (cost = c_pre)
      - post: the first snapshot AFTER the bucket advanced  (cost = c_post)

    The true crossing happened between them — we don't know when. Three ways
    to estimate the cost at the exact crossing moment:
      - pre:      c_pre                     (undershoots; cost hadn't reached boundary yet)
      - post:     c_post                    (overshoots; cost already past the boundary)
      - midpoint: (c_pre + c_post) / 2      (removes the systematic bias if calls are roughly uniform)

    Returns a list of crossing records, one per bucket transition.
    """
    if len(snaps) < 2:
        return []
    crossings = []
    prev = snaps[0]
    prev_bucket = int(prev[util_key] * 100 + 1e-9)
    for s in snaps[1:]:
        cur_bucket = int(s[util_key] * 100 + 1e-9)
        if cur_bucket > prev_bucket:
            # For multi-tick jumps, we can only bracket the *final* boundary
            # cleanly; intermediate boundaries have no pre-observation at that
            # exact bucket. Mark those as "coarse" so the caller can exclude
            # them from clean measurements.
            for k in range(prev_bucket + 1, cur_bucket + 1):
                coarse = (k != cur_bucket) or (cur_bucket - prev_bucket > 1)
                crossings.append({
                    "bucket": k,                    # tick boundary index (util × 100)
                    "boundary_util": k / 100.0,     # e.g. 0.73
                    "pre_label":  prev["label"],
                    "pre_util":   prev[util_key],
                    "pre_cost":   prev["cost_total"],
                    "post_label": s["label"],
                    "post_util":  s[util_key],
                    "post_cost":  s["cost_total"],
                    "cost_pre":      prev["cost_total"],
                    "cost_post":     s["cost_total"],
                    "cost_midpoint": (prev["cost_total"] + s["cost_total"]) / 2.0,
                    "coarse": coarse,
                })
            prev = s
            prev_bucket = cur_bucket
        else:
            # util unchanged — advance prev so cost keeps accumulating
            prev = s
    return crossings


def measured_ticks(crossings: list[dict], window_name: str) -> list[dict]:
    """
    Turn N+1 consecutive boundary crossings into N clean measured ticks.

    A "measured tick" is the interval between two consecutive boundary
    crossings where we have clean pre/post brackets for each end. Multi-tick
    jumps are excluded (the intermediate boundaries are "coarse" and can't
    be interpolated).
    """
    measured = []
    for i in range(len(crossings) - 1):
        start = crossings[i]
        end   = crossings[i + 1]
        if start["coarse"] or end["coarse"]:
            continue
        if end["bucket"] - start["bucket"] != 1:
            continue  # intervening boundaries; skip
        # Three per-tick capacity estimates — Δcost / 0.01 (since Δutil = 1 tick)
        cap_pre      = (end["cost_pre"]      - start["cost_pre"])      / 0.01
        cap_post     = (end["cost_post"]     - start["cost_post"])     / 0.01
        cap_midpoint = (end["cost_midpoint"] - start["cost_midpoint"]) / 0.01
        measured.append({
            "window":       window_name,
            "tick_from":    start["boundary_util"],   # e.g. 0.73
            "tick_to":      end["boundary_util"],     # e.g. 0.74
            "start_label":  start["post_label"],
            "end_label":    end["post_label"],
            "cap_pre_usd":      cap_pre,
            "cap_post_usd":     cap_post,
            "cap_midpoint_usd": cap_midpoint,
        })
    return measured


def tokens_from_usd(capacity_usd: float, price_per_mtok: float) -> int:
    return int(round(capacity_usd * 1_000_000 / price_per_mtok))


def fmt_tokens(n: int) -> str:
    return f"{n:,}"


def avg_capacity(ticks: list[dict], key: str) -> float | None:
    if not ticks:
        return None
    return sum(t[key] for t in ticks) / len(ticks)


def token_projection(capacity_usd: float) -> dict[str, dict[str, int]]:
    return {
        model: {
            "input_full_quota": tokens_from_usd(capacity_usd, pricing["input"]),
            "input_per_tick": tokens_from_usd(capacity_usd * 0.01, pricing["input"]),
            "output_full_quota": tokens_from_usd(capacity_usd, pricing["output"]),
            "output_per_tick": tokens_from_usd(capacity_usd * 0.01, pricing["output"]),
        }
        for model, pricing in PRICING.items()
    }


def build_bounds_summary(window: str, ticks: list[dict], proxy_capacity_usd: float) -> dict:
    low_pre = avg_capacity(ticks, "cap_pre_usd")
    midpoint = avg_capacity(ticks, "cap_midpoint_usd")
    high_post = avg_capacity(ticks, "cap_post_usd")
    bounded = low_pre is not None and midpoint is not None and high_post is not None
    # // [LAW:one-source-of-truth] bounds.json is the canonical derived summary
    # for quota bounds; report.md and CLI output are rendered from the same data.
    # // [LAW:single-enforcer] when there are no clean measured ticks, the
    # answer is "no measurement" — NOT a silent fallback to the proxy's
    # running estimate. The proxy estimate exists for runtime introspection;
    # using it as a substitute for measurement was the bug that hid runs that
    # produced zero clean ticks behind a number that looked authoritative.
    return {
        "window": window,
        "clean_measured_ticks": len(ticks),
        "proxy_lifetime_capacity_usd": proxy_capacity_usd,
        "weighted_usd": {
            "low_pre": low_pre,
            "midpoint": midpoint,
            "high_post": high_post,
            "selected": midpoint,  # None when no clean ticks — no fallback
        },
        "tokens": None if not bounded else {
            "low_pre": token_projection(low_pre),
            "midpoint": token_projection(midpoint),
            "high_post": token_projection(high_post),
        },
    }


def render_bounds_summary(bounds: dict, pinned_model_family: str) -> list[str]:
    weighted = bounds["weighted_usd"]
    lines = [
        f"{bounds['window']} bounds:",
        f"  clean measured ticks: {bounds['clean_measured_ticks']}",
    ]
    if weighted["midpoint"] is None:
        lines.append("  result: UNAVAILABLE — no clean measured ticks in this run")
        lines.append("    (a clean measurement requires two observed crossings; the first")
        lines.append("     establishes the bracket anchor, subsequent crossings each measure")
        lines.append("     one tick. Run longer to produce a measurement.)")
        proxy = bounds.get("proxy_lifetime_capacity_usd", 0.0)
        if proxy > 0:
            lines.append(f"  proxy lifetime accumulator: {proxy:.6f} weighted-USD "
                         "(diagnostic; NOT a substitute for a clean measurement)")
        return lines

    model_tokens = bounds["tokens"]["midpoint"][pinned_model_family]
    low_tokens = bounds["tokens"]["low_pre"][pinned_model_family]
    high_tokens = bounds["tokens"]["high_post"][pinned_model_family]
    lines.extend(
        [
            f"  weighted-USD: low={weighted['low_pre']:.6f} mid={weighted['midpoint']:.6f} high={weighted['high_post']:.6f}",
            f"  {pinned_model_family} input tokens: low={fmt_tokens(low_tokens['input_full_quota'])} mid={fmt_tokens(model_tokens['input_full_quota'])} high={fmt_tokens(high_tokens['input_full_quota'])}",
            f"  {pinned_model_family} input tokens / 1% tick: low={fmt_tokens(low_tokens['input_per_tick'])} mid={fmt_tokens(model_tokens['input_per_tick'])} high={fmt_tokens(high_tokens['input_per_tick'])}",
        ]
    )
    return lines


def render_probe_exit_summary(bounds: dict, pinned_model_family: str) -> list[str]:
    lines = [
        "Probe summary:",
        f"  run dir: {bounds['run_dir']}",
        f"  scope: org={bounds['org']} upstream={bounds['upstream']}",
        f"  model: {bounds['model']}",
    ]
    for window in ("5h", "7d"):
        summary = bounds["windows"][window]
        weighted = summary["weighted_usd"]
        lines.append(f"  {window} bounds:")
        if weighted["midpoint"] is None:
            lines.append("    result: UNAVAILABLE (no clean measured ticks in this run)")
            proxy = summary.get("proxy_lifetime_capacity_usd", 0.0)
            if proxy > 0:
                lines.append(f"    proxy_lifetime_accumulator={proxy:.6f} weighted-USD "
                             "(diagnostic only)")
        else:
            tokens = summary["tokens"]["midpoint"][pinned_model_family]
            lines.append(
                f"    low={weighted['low_pre']:.6f} midpoint={weighted['midpoint']:.6f} high={weighted['high_post']:.6f} weighted-USD"
            )
            lines.append(
                f"    {pinned_model_family}_input_tokens full_quota={fmt_tokens(tokens['input_full_quota'])} per_1pct_tick={fmt_tokens(tokens['input_per_tick'])}"
            )
    return lines


def main(run_dir: Path, print_bounds: bool) -> None:
    snaps = load_jsonl(run_dir / "snapshots.jsonl")
    iters = load_jsonl(run_dir / "iterations.jsonl")
    manifest = json.loads((run_dir / "manifest.json").read_text())

    if not snaps:
        raise SystemExit("report.py: need at least 1 snapshot")

    crossings_5h = detect_boundaries(snaps, "util_5h")
    crossings_7d = detect_boundaries(snaps, "util_7d")
    ticks_5h = measured_ticks(crossings_5h, "5h")
    ticks_7d = measured_ticks(crossings_7d, "7d")

    # Write derived JSONL files for audit
    (run_dir / "crossings.jsonl").write_text(
        "\n".join(json.dumps({**c, "window": "5h"}) for c in crossings_5h) +
        ("\n" if crossings_5h else "") +
        "\n".join(json.dumps({**c, "window": "7d"}) for c in crossings_7d) +
        ("\n" if crossings_7d else "")
    )
    (run_dir / "measured_ticks.jsonl").write_text(
        "\n".join(json.dumps(t) for t in (ticks_5h + ticks_7d)) +
        ("\n" if (ticks_5h or ticks_7d) else "")
    )

    first = snaps[0]
    last = snaps[-1]
    # Proxy-internal capacity (delta-method, accumulated lifetime)
    proxy_cap_5h = last["capacity_usd_5h"]
    proxy_cap_7d = last["capacity_usd_7d"]

    probe_cap_5h_mid  = avg_capacity(ticks_5h, "cap_midpoint_usd")
    probe_cap_5h_post = avg_capacity(ticks_5h, "cap_post_usd")
    probe_cap_5h_pre  = avg_capacity(ticks_5h, "cap_pre_usd")
    probe_cap_7d_mid  = avg_capacity(ticks_7d, "cap_midpoint_usd")
    probe_cap_7d_post = avg_capacity(ticks_7d, "cap_post_usd")
    probe_cap_7d_pre  = avg_capacity(ticks_7d, "cap_pre_usd")
    pinned_model_family = "opus" if "opus" in manifest["model"].lower() else "sonnet" if "sonnet" in manifest["model"].lower() else "haiku"
    bounds = {
        "run_dir": str(run_dir),
        "model": manifest["model"],
        "org": first.get("org"),
        "upstream": first.get("upstream"),
        "windows": {
            "5h": build_bounds_summary("5h", ticks_5h, proxy_cap_5h),
            "7d": build_bounds_summary("7d", ticks_7d, proxy_cap_7d),
        },
    }
    (run_dir / "bounds.json").write_text(json.dumps(bounds, indent=2) + "\n")

    lines: list[str] = []
    push = lines.append

    push("# Capacity Probe Report")
    push("")
    if manifest.get("dry_run"):
        push("> **DRY RUN** — `echo` was substituted for `claude`; no API calls were made. Tick counts below will be zero; this report exists to verify the probe's code paths, not to produce a capacity estimate.")
        push("")
    push(f"- Run directory: `{run_dir}`")
    push(f"- Started:       `{manifest['started']}` UTC")
    push(f"- Model pinned:  `{manifest['model']}`")
    push(f"- Metrics URL:   `{manifest['metrics_url']}`")
    push(f"- Iterations:    {len(iters)}")
    window = manifest.get("window", "—")
    push(f"- Window:        `{window}`")
    # New (single-window) manifest format uses target_ticks / required_crossings.
    # Older both-mode runs used target_{5h,7d}_ticks / required_crossings_{5h,7d};
    # render whichever form the manifest provides so historical reports keep working.
    if "target_ticks" in manifest:
        req = int(manifest.get("required_crossings", int(manifest["target_ticks"]) + 1))
        push(f"- Target {window} ticks: {manifest['target_ticks']} (needs {req} crossings)")
    else:
        req_5h = manifest.get("required_crossings_5h", int(manifest.get("target_5h_ticks", 0)) + 1)
        req_7d = manifest.get("required_crossings_7d", int(manifest.get("target_7d_ticks", 0)) + 1)
        push(f"- Target 5h ticks: {manifest.get('target_5h_ticks', '—')} (needs {req_5h} crossings)")
        push(f"- Target 7d ticks: {manifest.get('target_7d_ticks', '—')} (needs {req_7d} crossings)")
    push(f"- Observed 5h crossings: {len(crossings_5h)} → {len(ticks_5h)} clean measured tick(s)")
    push(f"- Observed 7d crossings: {len(crossings_7d)} → {len(ticks_7d)} clean measured tick(s)")
    push("")
    push("## Baseline → Final")
    push("")
    push("| Field   | Baseline        | Final           | Δ              |")
    push("|---------|-----------------|-----------------|----------------|")
    push(f"| util_5h | {first['util_5h']}        | {last['util_5h']}        | +{last['util_5h']-first['util_5h']:.4f}       |")
    push(f"| util_7d | {first['util_7d']}        | {last['util_7d']}        | +{last['util_7d']-first['util_7d']:.4f}       |")
    push(f"| cost    | {first['cost_total']:.6f} | {last['cost_total']:.6f} | +{last['cost_total']-first['cost_total']:.6f} |")
    push("")
    push("## Capacity Estimates")
    push("")
    push("Internal unit: weighted-price-dollar-equivalent. Three estimates are compared:")
    push("")
    push("- **Midpoint** — recommended. For each tick boundary K, estimate the true cost at K as `(c_pre + c_post) / 2`. Per-tick capacity = `(mid_K+1 − mid_K) / 0.01`. Removes the systematic bias that pre/post alone have.")
    push("- **Post-post** — uses the first snapshot after each boundary crossing. Overshoots (bias: high) but partially cancels if call sizes are uniform.")
    push("- **Pre-pre** — uses the last snapshot before each boundary crossing. Undershoots (bias: low).")
    push("")
    push(f"Proxy lifetime accumulator (exposed at `/metrics`, accumulated across all clean per-tick measurements since the proxy started — leading-bracket cost is excluded; see `metrics.go` `updateCapacityEstimate`):")
    push("")
    push(f"- 5h accumulator (proxy): `{proxy_cap_5h:.6f}` (weighted-USD per tick)")
    push(f"- 7d accumulator (proxy): `{proxy_cap_7d:.6f}` (weighted-USD per tick)")
    push("")
    push("This number is diagnostic only. The authoritative result for this run is the per-tick value computed below from this run's clean measured ticks (if any).")
    push("")
    push("### Probe-derived capacity (this run only)")
    push("")
    push("| Window | Method     | Capacity (weighted-USD) | # ticks |")
    push("|--------|------------|-------------------------|---------|")
    for label, mid, post, pre, n in [
        ("5h", probe_cap_5h_mid, probe_cap_5h_post, probe_cap_5h_pre, len(ticks_5h)),
        ("7d", probe_cap_7d_mid, probe_cap_7d_post, probe_cap_7d_pre, len(ticks_7d)),
    ]:
        for method, val in [("midpoint", mid), ("post-post", post), ("pre-pre", pre)]:
            cell = f"{val:.6f}" if val is not None else "—"
            push(f"| {label} | {method:<10} | {cell:>23} | {n:>7} |")
    push("")
    push("### Bounds")
    push("")
    push("Low = pre-pre, midpoint = recommended, high = post-post.")
    push("")
    for window in ("5h", "7d"):
        summary = bounds["windows"][window]
        weighted = summary["weighted_usd"]
        push(f"#### {window}")
        push("")
        if weighted["midpoint"] is None:
            push("_(no clean measured ticks in this run — result UNAVAILABLE. "
                 "A clean measurement requires two observed crossings.)_")
            push("")
            continue
        pinned_mid = summary["tokens"]["midpoint"][pinned_model_family]
        pinned_low = summary["tokens"]["low_pre"][pinned_model_family]
        pinned_high = summary["tokens"]["high_post"][pinned_model_family]
        push("| Bound | Weighted-USD | Input tokens (full quota) | Input tokens / 1% tick |")
        push("|-------|--------------|---------------------------|------------------------|")
        push(f"| Low   | {weighted['low_pre']:.6f} | {fmt_tokens(pinned_low['input_full_quota'])} | {fmt_tokens(pinned_low['input_per_tick'])} |")
        push(f"| Mid   | {weighted['midpoint']:.6f} | {fmt_tokens(pinned_mid['input_full_quota'])} | {fmt_tokens(pinned_mid['input_per_tick'])} |")
        push(f"| High  | {weighted['high_post']:.6f} | {fmt_tokens(pinned_high['input_full_quota'])} | {fmt_tokens(pinned_high['input_per_tick'])} |")
        push("")
    push("### Capacity as tokens (midpoint estimate, this run)")
    push("")
    for label, mid in [("5h", probe_cap_5h_mid), ("7d", probe_cap_7d_mid)]:
        push(f"#### {label}")
        push("")
        if mid is None:
            push("_(no clean measured ticks in this run — result UNAVAILABLE.)_")
            push("")
            continue
        push("| Model   | Input tokens        | Output tokens       |")
        push("|---------|---------------------|---------------------|")
        for m in ("haiku", "sonnet", "opus"):
            ti = tokens_from_usd(mid, PRICING[m]["input"])
            to = tokens_from_usd(mid, PRICING[m]["output"])
            push(f"| {m:<7} | {fmt_tokens(ti):>19} | {fmt_tokens(to):>19} |")
        push("")
    push("## Measured Ticks (per-tick breakdown)")
    push("")
    push("One row per clean measured tick. A clean tick is bracketed by two consecutive boundary crossings with no multi-tick jumps. Compare the three capacity columns to see the spread — they should cluster tightly if the probe is well-calibrated.")
    push("")

    def _tick_table(name: str, rows: list[dict]) -> None:
        push(f"### {name}")
        push("")
        if not rows:
            push("_(no clean measured ticks in this run)_")
            push("")
            return
        push("| # | tick        | cap_midpoint | cap_post | cap_pre |")
        push("|---|-------------|--------------|----------|---------|")
        for i, t in enumerate(rows, 1):
            push(f"| {i} | {t['tick_from']:.2f} → {t['tick_to']:.2f} | {t['cap_midpoint_usd']:.4f}     | {t['cap_post_usd']:.4f} | {t['cap_pre_usd']:.4f} |")
        push("")

    _tick_table("5h", ticks_5h)
    _tick_table("7d", ticks_7d)

    push("## Provenance / audit trail")
    push("")
    push("- `manifest.json`        — run config, baseline snapshot")
    push("- `snapshots.jsonl`      — every `/metrics` scrape (parsed JSON)")
    push("- `raw-metrics/`         — verbatim Prometheus exposition bodies")
    push("- `iterations.jsonl`     — every `claude -p` invocation (prompt, exit, wall time)")
    push("- `prompts/`             — exact prompt text sent per iteration")
    push("- `claude-output/`       — literal stdout+stderr per iteration")
    push("- `crossings.jsonl`      — every detected tick-boundary crossing (derived)")
    push("- `measured_ticks.jsonl` — clean measured ticks with all three capacity estimates (derived)")
    push("- `bounds.json`          — machine-readable low/mid/high bounds (derived)")
    push("- `probe.sh`             — thin shell wrapper as-run")
    push("- `probe.py`             — the Python probe driver as-run")
    push("- `report.py`            — this script as-run")
    push("- `scripts.sha256`       — SHA-256 of driver and report script")
    push("")
    push("The canonical source is `snapshots.jsonl` + `raw-metrics/`. Everything else is reproducible by re-running `report.py <run_dir>`.")
    push("")

    (run_dir / "report.md").write_text("\n".join(lines))
    if print_bounds:
        for line in render_probe_exit_summary(bounds, pinned_model_family):
            print(line)
        for window in ("5h", "7d"):
            for line in render_bounds_summary(bounds["windows"][window], pinned_model_family):
                print(line)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir")
    parser.add_argument("--print-bounds", action="store_true")
    args = parser.parse_args()
    main(Path(args.run_dir), args.print_bounds)
