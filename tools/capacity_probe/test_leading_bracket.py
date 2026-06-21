#!/usr/bin/env python3
"""Postflight + report.py contract tests.

Originally guarded the *legacy* leading-bracket exclusion in the postflight
headline (mean-of-midpoints aggregator that had to drop the leading bracket
because Q₀ was unmodeled). With nerf-convergent-probe-xkh.2, the headline
became `to_ocw(estimate_C(crossings, prior).mid)` — Q₀ cancels under pairwise
subtraction so the leading bracket now CONTRIBUTES as an anchor rather than
being excluded. These tests are rewritten to lock in the new contract:

  - Every observed Crossing contributes to the headline (no exclusion).
  - Zero crossings ⇒ "Insufficient data — no crossings observed".
  - In-flight blocks have no associated Crossing, so they cannot move the
    headline (and the panel still surfaces them as excluded blocks).
  - Disjoint constraints surface as a first-class diagnostic, not a crash.

The report.py bounds-summary tests at the bottom of this file remain
unchanged — they cover a separate concern and are still load-bearing.

Run with:
    uv run --with rich python -m pytest tools/capacity_probe/test_leading_bracket.py -v
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def probe():
    """Load probe.py as a module so we can poke at its internals directly."""
    here = Path(__file__).parent
    spec = importlib.util.spec_from_file_location("probe", here / "probe.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["probe"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def report():
    here = Path(__file__).parent
    spec = importlib.util.spec_from_file_location("report", here / "report.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["report"] = m
    spec.loader.exec_module(m)
    return m


def _summary(probe, *, tick_num, util_pre, util_post, tokens_input_equiv,
             last_iter_input_equiv=20000.0, wall_s=10.0, iter_count=3,
             crossed=1, is_leading_bracket=False):
    """Build a _PerTickSummary; tokens passed in input-equivalent units."""
    return probe._PerTickSummary(
        tick_num=tick_num,
        util_pre=util_pre,
        util_post=util_post,
        units=tokens_input_equiv,
        last_iter_units_before_cross=last_iter_input_equiv,
        wall_s=wall_s,
        iter_nums=list(range(1, iter_count + 1)),
        crossed=crossed,
        is_leading_bracket=is_leading_bracket,
    )


def _render(post_group, width=160) -> str:
    """Capture the rendered post-flight text for assertion."""
    import io
    from rich.console import Console
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=width).print(post_group)
    return buf.getvalue()


def _prior_for(probe, window: str = "5h"):
    """Construct the canonical prior interval for a window — same shape the
    live loop uses, so test assertions match the panel arithmetic exactly."""
    from crossings import Interval
    default_C = probe.DEFAULT_INPUT_EQUIV_PER_TICK[window]
    return Interval(
        lo=default_C * (1.0 - probe.PRIOR_C_SLOP),
        hi=default_C * (1.0 + probe.PRIOR_C_SLOP),
    )


def _crossing(k: int, Y_before: float, Y_after: float, iter_num: int):
    from crossings import Crossing
    return Crossing(k=k, Y_before=Y_before, Y_after=Y_after, iter_num=iter_num)


def test_post_flight_leading_bracket_contributes_via_pairwise(probe):
    """The leading bracket is no longer excluded — it anchors every pair it
    participates in. With the new estimator, the headline value is the
    midpoint of `estimate_C(all_crossings, prior)`, and the leading bracket
    is the lowest-k Crossing that contributes to N-1 pairs."""
    from crossings import estimate_C
    pre = probe._PreFlightSummary(
        model="claude-opus-4-7", window="5h", util_pct_baseline=5,
        target_ticks=2, required_crossings=3,
        est_tokens_per_tick=200_000.0, expected_wall_s=60.0,
    )
    # Three crossings with a clean ~400_000 input-equiv per-tick signal.
    # The leading bracket's bracket width is intentionally wider (its iter
    # was longer reaching the first crossing) — but it still contributes.
    crossings = [
        _crossing(k=6, Y_before=800_000, Y_after=1_000_000, iter_num=3),    # leading
        _crossing(k=7, Y_before=1_350_000, Y_after=1_400_000, iter_num=5),  # tick 1
        _crossing(k=8, Y_before=1_750_000, Y_after=1_800_000, iter_num=7),  # tick 2
    ]
    pt = [
        _summary(probe, tick_num=0, util_pre=5, util_post=6,
                 tokens_input_equiv=1_000_000.0, is_leading_bracket=True),
        _summary(probe, tick_num=1, util_pre=6, util_post=7,
                 tokens_input_equiv=400_000.0),
        _summary(probe, tick_num=2, util_pre=7, util_post=8,
                 tokens_input_equiv=400_000.0),
    ]
    prior = _prior_for(probe)
    expected_mid_ocw = int(round(probe.to_ocw(estimate_C(crossings, prior).mid)))

    out = _render(probe._render_postflight(
        pre=pre, per_tick=pt, total_wall_s=42.0, interrupted=False,
        crossings=crossings, prior=prior,
    ))

    assert f"{expected_mid_ocw:,}" in out, (
        f"headline should be {expected_mid_ocw:,} (= to_ocw(estimate_C.mid)):\n{out}"
    )
    assert "tokens per 1% tick" in out
    assert "3 crossings" in out, "should report the count of contributing Crossings"
    assert "leading bracket" in out, "should still surface the leading bracket as a note"
    assert "Contributes as an anchor" in out, (
        "leading-bracket note should advertise pairwise contribution, not exclusion"
    )
    # Legacy "excluded (leading bracket)" framing should be gone.
    assert "excluded (leading bracket)" not in out


def test_post_flight_no_crossings_says_insufficient(probe):
    """Zero crossings ⇒ headline must be 'Insufficient data — no crossings
    observed', and never a fabricated per-tick number."""
    pre = probe._PreFlightSummary(
        model="claude-opus-4-7", window="5h", util_pct_baseline=5,
        target_ticks=2, required_crossings=3,
        est_tokens_per_tick=200_000.0, expected_wall_s=60.0,
    )
    pt: list = []  # nothing closed
    prior = _prior_for(probe)
    out = _render(probe._render_postflight(
        pre=pre, per_tick=pt, total_wall_s=15.0, interrupted=False,
        crossings=[], prior=prior,
    ))

    assert "Insufficient data" in out, f"expected explicit 'Insufficient data':\n{out}"
    assert "no crossings observed" in out
    assert "tokens per 1% tick" not in out, "must not print a per-tick number"


def test_post_flight_in_flight_block_does_not_move_headline(probe):
    """An in-flight block (interrupt or DRY RUN cap) produces no Crossing,
    so it cannot move the headline. The panel must still surface it as an
    excluded block."""
    from crossings import estimate_C
    pre = probe._PreFlightSummary(
        model="claude-opus-4-7", window="5h", util_pct_baseline=5,
        target_ticks=2, required_crossings=3,
        est_tokens_per_tick=200_000.0, expected_wall_s=60.0,
    )
    crossings = [
        _crossing(k=6, Y_before=800_000, Y_after=1_000_000, iter_num=3),
        _crossing(k=7, Y_before=1_350_000, Y_after=1_400_000, iter_num=5),
    ]
    pt = [
        _summary(probe, tick_num=0, util_pre=5, util_post=6,
                 tokens_input_equiv=1_000_000.0, is_leading_bracket=True),
        _summary(probe, tick_num=1, util_pre=6, util_post=7,
                 tokens_input_equiv=400_000.0),
        _summary(probe, tick_num=2, util_pre=7, util_post=7,
                 tokens_input_equiv=100_000.0, crossed=0),  # in flight
    ]
    prior = _prior_for(probe)
    expected_mid_ocw = int(round(probe.to_ocw(estimate_C(crossings, prior).mid)))
    out = _render(probe._render_postflight(
        pre=pre, per_tick=pt, total_wall_s=42.0, interrupted=True,
        crossings=crossings, prior=prior,
    ))

    assert f"{expected_mid_ocw:,}" in out, (
        f"headline should be {expected_mid_ocw:,}:\n{out}"
    )
    assert "2 crossings" in out
    assert "excluded (in flight)" in out


def test_post_flight_disjoint_constraints_surface_diagnostic(probe):
    """If estimate_C raises ValueError (disjoint pairwise constraints, or
    constraints disjoint with the prior), the panel must render a
    first-class diagnostic — not crash."""
    pre = probe._PreFlightSummary(
        model="claude-opus-4-7", window="5h", util_pct_baseline=5,
        target_ticks=2, required_crossings=3,
        est_tokens_per_tick=200_000.0, expected_wall_s=60.0,
    )
    # Pairwise constraint says C ≈ 420_000; prior here is deliberately tight
    # at [100_000, 200_000] so the intersection is empty.
    from crossings import Interval
    crossings = [
        _crossing(k=30, Y_before=80_000, Y_after=100_000, iter_num=1),
        _crossing(k=31, Y_before=500_000, Y_after=520_000, iter_num=2),
    ]
    tight_prior = Interval(100_000, 200_000)
    pt: list = []  # no per-tick rows needed for this assertion
    out = _render(probe._render_postflight(
        pre=pre, per_tick=pt, total_wall_s=30.0, interrupted=False,
        crossings=crossings, prior=tight_prior,
    ))

    assert "Disjoint constraints" in out, f"expected disjoint diagnostic:\n{out}"
    assert "tokens per 1% tick" not in out, "must not fabricate a per-tick number on failure"


def test_build_bounds_summary_no_fallback_to_proxy(report):
    """When clean_measured_ticks is 0, build_bounds_summary must return
    midpoint=None and selected=None — it must NOT silently fall back to the
    proxy lifetime estimate. That fallback was the bug that made invalid
    runs look authoritative."""
    bounds = report.build_bounds_summary("5h", ticks=[], proxy_capacity_usd=999.0)
    assert bounds["clean_measured_ticks"] == 0
    assert bounds["weighted_usd"]["midpoint"] is None
    assert bounds["weighted_usd"]["selected"] is None, \
        "selected must be None when there are no clean ticks (no fallback to proxy)"
    assert bounds["proxy_lifetime_capacity_usd"] == 999.0, \
        "proxy lifetime is preserved as a diagnostic field"
    assert bounds["tokens"] is None


def test_build_bounds_summary_with_clean_ticks(report):
    """With clean ticks, build_bounds_summary returns the measured midpoint."""
    ticks = [
        {"cap_pre_usd": 200.0, "cap_midpoint_usd": 199.0, "cap_post_usd": 198.0},
        {"cap_pre_usd": 202.0, "cap_midpoint_usd": 201.0, "cap_post_usd": 200.0},
    ]
    bounds = report.build_bounds_summary("5h", ticks=ticks, proxy_capacity_usd=999.0)
    assert bounds["clean_measured_ticks"] == 2
    assert bounds["weighted_usd"]["midpoint"] == 200.0
    assert bounds["weighted_usd"]["selected"] == 200.0
    assert bounds["tokens"] is not None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
