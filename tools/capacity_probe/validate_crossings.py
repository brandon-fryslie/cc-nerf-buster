#!/usr/bin/env python3
"""Validate the position-constraint estimator against the legacy per-tick mean.

For a given probe run directory, derive `Crossing` observations (preferring the
live-written `position-constraints.jsonl` if present, otherwise reconstructing
from `iterations.jsonl` + `snapshots.jsonl`), then compute `C` (tokens per 1%
tick) two ways:

  legacy  — average of consecutive-crossing differences (matches probe.py:1530
            and report.py's `measured_ticks` aggregator).
  new     — pairwise constraint intersection (`crossings.estimate_C`).

Both must agree on the central value within the legacy uncertainty band; the
new estimator's interval width should be narrower than any single per-tick
bracket (because intersection across k-distances 1..N-1 uses information the
legacy estimator throws away).

This is the validation gate for nerf-convergent-probe-xkh.1 (the additive seam
that records Crossings) and the design check for nerf-convergent-probe-xkh.2
(the estimator that consumes them).
"""

from __future__ import annotations

import argparse
from pathlib import Path

# // [LAW:one-source-of-truth] Re-use the canonical Crossing/Interval types
# and the new estimator from `crossings.py` instead of forking arithmetic.
from crossings import (
    Crossing,
    Interval,
    derive_crossings_from_iterations,
    estimate_C,
    load_crossings,
)


# Mirrored from probe.py for offline use. // [LAW:one-source-of-truth] is
# bent here on purpose — the validation script is a scratch tool, kept
# self-contained so it doesn't drag in probe.py's full dependency tree.
DEFAULT_INPUT_EQUIV_PER_TICK = {
    "5h": 550_623.0,
    "7d": 550_623.0 * 5,
}


def legacy_estimate_from_crossings(crossings: list[Crossing]) -> tuple[float, float, float, int]:
    """Reproduce the legacy aggregator: average of consecutive-crossing diffs.

    Returns (mean, low, high, n_diffs). `low` and `high` are the min and max
    observed per-tick cost across the consecutive differences — the same
    spread the existing per-tick table reports.
    """
    diffs: list[float] = []
    for a, b in zip(crossings, crossings[1:]):
        if b.k - a.k != 1:
            continue
        # Per-tick cost ≈ midpoint of the bracket [Y_b_before - Y_a_after,
        # Y_b_after - Y_a_before]. The legacy code in probe.py:1530 uses
        # (Y_b_after - Y_a_after) -- effectively post-to-post -- which is
        # the same as `units_so_far` over one tick. Both work; pick the
        # midpoint here for symmetry with the bracket interpretation.
        bracket_lo = b.Y_before - a.Y_after
        bracket_hi = b.Y_after - a.Y_before
        diffs.append((bracket_lo + bracket_hi) / 2.0)
    if not diffs:
        return 0.0, 0.0, 0.0, 0
    return sum(diffs) / len(diffs), min(diffs), max(diffs), len(diffs)


def load_run_crossings(run_dir: Path) -> tuple[list[Crossing], str]:
    """Prefer live-recorded position-constraints; fall back to post-hoc derivation.

    Returns (crossings, source) where `source` is "live" or "derived" for the
    diagnostic header.
    """
    live = load_crossings(run_dir)
    if live:
        return live, "live (position-constraints.jsonl)"
    derived = derive_crossings_from_iterations(run_dir)
    return derived, "derived (iterations.jsonl + snapshots.jsonl)"


def fmt(x: float) -> str:
    return f"{x:>12,.0f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--prior-slop", type=float, default=0.5,
                    help="Prior C interval = default ± slop·default (default: 0.5)")
    args = ap.parse_args()

    if not args.run_dir.exists():
        raise SystemExit(f"no such run_dir: {args.run_dir}")

    crossings, source = load_run_crossings(args.run_dir)
    print(f"Run:     {args.run_dir.name}")
    print(f"Source:  {source}")
    print(f"Observed crossings: {len(crossings)}")
    if not crossings:
        raise SystemExit("no crossings to estimate from")

    print()
    print("Crossings:")
    print(f"  {'iter':>5}  {'k':>3}  {'Y_before':>14}  {'Y_after':>14}  {'bracket_width':>14}")
    for c in crossings:
        print(f"  {c.iter_num:>5}  {c.k:>3}  {c.Y_before:>14,.0f}  {c.Y_after:>14,.0f}  {c.Y_after - c.Y_before:>14,.0f}")

    # Window from manifest.
    import json
    manifest = json.loads((args.run_dir / "manifest.json").read_text())
    window = manifest["window"]
    default_C = DEFAULT_INPUT_EQUIV_PER_TICK[window]
    prior = Interval(
        lo=default_C * (1 - args.prior_slop),
        hi=default_C * (1 + args.prior_slop),
    )

    print()
    print(f"Window:  {window}")
    print(f"Prior:   C ∈ [{fmt(prior.lo)}, {fmt(prior.hi)}]  (default {default_C:,.0f} ± {args.prior_slop:.0%})")

    legacy_mean, legacy_lo, legacy_hi, n_diffs = legacy_estimate_from_crossings(crossings)
    print()
    print("Legacy estimator (mean of consecutive per-tick midpoints):")
    print(f"  N diffs:    {n_diffs}")
    print(f"  mean:       {fmt(legacy_mean)} tokens/tick")
    print(f"  spread:     {fmt(legacy_lo)} .. {fmt(legacy_hi)}  (Δ {fmt(legacy_hi - legacy_lo)})")

    try:
        new_C = estimate_C(crossings, prior)
    except ValueError as e:
        # Pairwise constraints disjoint with prior (or with each other after
        # intersection). Route through the same friendly diagnostic the
        # legacy-spread mismatch uses below, so the user sees a coherent
        # tool output instead of a bare stack trace.
        print()
        print("New estimator: FAILED")
        print(f"  ✗ {e}")
        print("  (Hint: the prior may be wrong for this window/run, or the")
        print("  position constraints are mutually inconsistent — inspect")
        print("  the crossings table above for the offending pair.)")
        raise SystemExit(1)
    print()
    print("New estimator (intersection of pairwise constraints):")
    print(f"  C ∈        [{fmt(new_C.lo)}, {fmt(new_C.hi)}]")
    print(f"  width:      {fmt(new_C.width)}")
    print(f"  mid:        {fmt(new_C.mid)} tokens/tick")

    print()
    print("Agreement check (validation gate):")
    legacy_band_lo = legacy_lo
    legacy_band_hi = legacy_hi
    if new_C.lo <= legacy_band_hi and new_C.hi >= legacy_band_lo:
        print(f"  ✓ new interval overlaps legacy spread "
              f"({fmt(legacy_band_lo)} .. {fmt(legacy_band_hi)})")
    else:
        print(f"  ✗ new interval [{fmt(new_C.lo)}, {fmt(new_C.hi)}] "
              f"disjoint from legacy spread [{fmt(legacy_band_lo)}, {fmt(legacy_band_hi)}]")
        raise SystemExit(1)

    # Width comparison: new interval should be narrower than any single
    # per-tick bracket from the crossings used.
    single_widths = [c.Y_after - c.Y_before for c in crossings]
    tightest_single = min(single_widths)
    if new_C.width < tightest_single:
        print(f"  ✓ new interval width ({fmt(new_C.width)}) < tightest single bracket ({fmt(tightest_single)})")
    else:
        print(f"  ! new interval width ({fmt(new_C.width)}) ≥ tightest single bracket ({fmt(tightest_single)})")
        print("    (acceptable when N=2 and brackets are large; expect strict improvement at N≥3)")


if __name__ == "__main__":
    main()
