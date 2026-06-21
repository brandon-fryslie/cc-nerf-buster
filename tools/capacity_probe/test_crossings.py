#!/usr/bin/env python3
"""Tests for the Crossing/Interval seam in crossings.py.

Run with:
    uv run --with pytest python -m pytest tools/capacity_probe/test_crossings.py -v
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from crossings import (
    Crossing,
    Interval,
    POSITION_CONSTRAINTS_FILENAME,
    build_crossings,
    estimate_C,
    load_crossings,
    write_crossings,
)


# -------- Interval ------------------------------------------------------------


def test_interval_intersect_narrows():
    a = Interval(100, 200)
    b = Interval(150, 250)
    assert a.intersect(b) == Interval(150, 200)


def test_interval_intersect_disjoint_raises():
    with pytest.raises(ValueError, match="Disjoint"):
        Interval(0, 50).intersect(Interval(100, 200))


def test_interval_invalid_construction_raises():
    with pytest.raises(ValueError, match="lo <= hi"):
        Interval(200, 100)


# -------- build_crossings -----------------------------------------------------


def test_build_single_tick():
    out = build_crossings(util_pct_pre=29, util_pct_post=30,
                          Y_before=100.0, Y_after=200.0, iter_num=5)
    assert len(out) == 1
    assert out[0].k == 30
    assert out[0].Y_before == 100.0
    assert out[0].Y_after == 200.0
    assert out[0].multi_tick_group == 0


def test_build_no_crossing_returns_empty():
    out = build_crossings(util_pct_pre=29, util_pct_post=29,
                          Y_before=100.0, Y_after=200.0, iter_num=5)
    assert out == []


def test_build_multi_tick_shares_group_id():
    out = build_crossings(util_pct_pre=29, util_pct_post=32,
                          Y_before=100.0, Y_after=200.0, iter_num=7)
    assert [c.k for c in out] == [30, 31, 32]
    # All three share the same nonzero group id (= iter_num)
    assert {c.multi_tick_group for c in out} == {7}


# -------- write/load round-trip (the "live recording" path) -------------------


def test_write_load_roundtrip(tmp_path: Path):
    crossings = [
        Crossing(k=30, Y_before=100.0, Y_after=200.0, iter_num=5),
        Crossing(k=31, Y_before=300.0, Y_after=400.0, iter_num=7),
    ]
    write_crossings(tmp_path, crossings)
    loaded = load_crossings(tmp_path)
    assert loaded == crossings


def test_write_appends_not_truncates(tmp_path: Path):
    # First write
    write_crossings(tmp_path, [Crossing(k=30, Y_before=100.0, Y_after=200.0, iter_num=5)])
    # Second write (simulating a subsequent crossing during the same run)
    write_crossings(tmp_path, [Crossing(k=31, Y_before=300.0, Y_after=400.0, iter_num=7)])
    loaded = load_crossings(tmp_path)
    assert len(loaded) == 2
    assert [c.k for c in loaded] == [30, 31]


def test_write_empty_no_file_created(tmp_path: Path):
    write_crossings(tmp_path, [])
    assert not (tmp_path / POSITION_CONSTRAINTS_FILENAME).exists()


def test_write_jsonl_one_record_per_line(tmp_path: Path):
    write_crossings(tmp_path, [
        Crossing(k=30, Y_before=100.0, Y_after=200.0, iter_num=5),
        Crossing(k=31, Y_before=300.0, Y_after=400.0, iter_num=7),
    ])
    raw = (tmp_path / POSITION_CONSTRAINTS_FILENAME).read_text()
    lines = [l for l in raw.split("\n") if l]
    assert len(lines) == 2
    for line in lines:
        assert json.loads(line)  # parses as JSON


# -------- estimate_C ---------------------------------------------------------


def test_estimate_C_brackets_true_value():
    # Construct synthetic data with known C = 180_000. Q0 = 5_310_000.
    C_true = 180_000.0
    Q0 = 5_310_000.0
    # For each k, Y_boundary = k*C - Q0. Pretend we crossed mid-iter
    # with a 20k-token last iter on each crossing.
    crossings = []
    for k in (30, 31, 32):
        y_boundary = k * C_true - Q0
        crossings.append(Crossing(k=k, Y_before=y_boundary - 10_000,
                                  Y_after=y_boundary + 10_000, iter_num=k))
    est = estimate_C(crossings, Interval(100_000, 300_000))
    assert est.lo <= C_true <= est.hi


def test_estimate_C_skips_same_multi_tick_group():
    # Two crossings sharing one bracket would otherwise yield [-W, +W] noise
    # that conflicts with a positive prior. Filtered out → prior survives.
    crossings = [
        Crossing(k=40, Y_before=100.0, Y_after=200.0, iter_num=99, multi_tick_group=99),
        Crossing(k=41, Y_before=100.0, Y_after=200.0, iter_num=99, multi_tick_group=99),
    ]
    prior = Interval(100_000, 300_000)
    est = estimate_C(crossings, prior)
    # Same-group pair contributes no constraint; result is just the prior.
    assert est == prior


def test_estimate_C_disjoint_raises():
    # Two crossings whose constraints on C cannot both hold for any single C.
    crossings = [
        Crossing(k=30, Y_before=80_000, Y_after=100_000, iter_num=1),
        Crossing(k=31, Y_before=500_000, Y_after=520_000, iter_num=2),
    ]
    # Pair (30, 31): C in [400_000, 440_000].
    # Prior says C in [100_000, 200_000] — disjoint with pair constraint.
    with pytest.raises(ValueError, match="Disjoint"):
        estimate_C(crossings, Interval(100_000, 200_000))


def test_estimate_C_empty_returns_prior():
    prior = Interval(100_000, 300_000)
    assert estimate_C([], prior) == prior
