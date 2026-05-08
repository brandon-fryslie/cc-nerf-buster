#!/usr/bin/env python3
"""Tests for leading-bracket exclusion in probe.py UI and report.py.

The leading bracket — the cost spent reaching the first observed crossing —
is NOT a measurement. Its starting position inside the integer percent is
unknown, so the cost spans an unknown sub-percent slice (anywhere from 0 to
~1 tick). Accumulating it as if it were 1 tick worth was the bug that
inflated both the post-flight headline and the proxy's running estimator
by random partial-tick noise.

These tests are the regression guard. Run with:
    uv run --with rich python -m pytest tools/capacity-probe/test_leading_bracket.py -v
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


def test_post_flight_excludes_leading_bracket_from_headline(probe):
    """The leading bracket (tick_num=0, is_leading_bracket=True) must NOT
    contribute to the headline tokens-per-tick value. The headline equals
    the average of the clean measurements only.

    Leading: 400000 input-equiv (= 200K OCW)
    Tick 1: 388000 (= 194K OCW)
    Tick 2: 414000 (= 207K OCW)

    Wrong headline (includes leading): (200K+194K+207K)/3 = 200.33K
    Correct headline (excludes leading): (194K+207K)/2 = 200.5K

    Test uses values where the leading bracket would PULL the headline
    significantly off if it were wrongly included."""
    pre = probe._PreFlightSummary(
        model="claude-opus-4-7", window="5h", util_pct_baseline=5,
        target_ticks=2, required_crossings=3,
        est_tokens_per_tick=200_000.0, expected_wall_s=60.0,
    )
    pt = [
        # Leading bracket at very different value — would pollute the headline if included
        _summary(probe, tick_num=0, util_pre=5, util_post=6,
                 tokens_input_equiv=1_000_000.0, is_leading_bracket=True),
        _summary(probe, tick_num=1, util_pre=6, util_post=7,
                 tokens_input_equiv=388_000.0),
        _summary(probe, tick_num=2, util_pre=7, util_post=8,
                 tokens_input_equiv=414_000.0),
    ]
    out = _render(probe._render_postflight(pre=pre, per_tick=pt, total_wall_s=42.0, interrupted=False))

    # Clean measurements: 388K and 414K input-equiv = 194K and 207K OCW.
    # Average = 200.5K OCW. If the headline were wrongly using the leading
    # bracket (500K OCW from the 1M input-equiv), it would land near 300K.
    assert "200,500" in out, f"headline should be 200,500 (mean of clean measurements only):\n{out}"
    assert "300," not in out, f"leading bracket leaked into headline:\n{out}"
    assert "2 clean measurements" in out, "should label count as clean measurements"
    assert "excluded (leading bracket)" in out, "should surface the leading bracket as excluded"


def test_post_flight_no_clean_measurements_says_unavailable(probe):
    """If the run produced only the leading bracket (or nothing), the headline
    must report INSUFFICIENT DATA, not a number."""
    pre = probe._PreFlightSummary(
        model="claude-opus-4-7", window="5h", util_pct_baseline=5,
        target_ticks=2, required_crossings=3,
        est_tokens_per_tick=200_000.0, expected_wall_s=60.0,
    )
    pt = [_summary(probe, tick_num=0, util_pre=5, util_post=6,
                   tokens_input_equiv=400_000.0, is_leading_bracket=True)]
    out = _render(probe._render_postflight(pre=pre, per_tick=pt, total_wall_s=15.0, interrupted=False))

    assert "Insufficient data" in out, f"expected explicit 'Insufficient data' label:\n{out}"
    assert "no clean measurements" in out
    # No fake headline number.
    assert "tokens per 1% tick" not in out, f"must not print a per-tick number:\n{out}"


def test_post_flight_with_in_flight_block_excluded(probe):
    """An in-flight block (interrupt or DRY RUN cap) is not a measurement
    and must be excluded from the headline."""
    pre = probe._PreFlightSummary(
        model="claude-opus-4-7", window="5h", util_pct_baseline=5,
        target_ticks=2, required_crossings=3,
        est_tokens_per_tick=200_000.0, expected_wall_s=60.0,
    )
    pt = [
        _summary(probe, tick_num=0, util_pre=5, util_post=6,
                 tokens_input_equiv=400_000.0, is_leading_bracket=True),
        _summary(probe, tick_num=1, util_pre=6, util_post=7,
                 tokens_input_equiv=400_000.0),
        # In flight at the time the run ended:
        _summary(probe, tick_num=2, util_pre=7, util_post=7,
                 tokens_input_equiv=100_000.0, crossed=0),
    ]
    out = _render(probe._render_postflight(pre=pre, per_tick=pt, total_wall_s=42.0, interrupted=True))

    # Only the one clean measurement (400K input-equiv = 200K OCW) goes into the headline.
    assert "200,000" in out, f"headline should be 200,000:\n{out}"
    assert "1 clean measurement" in out
    assert "excluded (in flight)" in out


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
