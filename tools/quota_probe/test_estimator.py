#!/usr/bin/env python3
from __future__ import annotations

from tools.quota_probe.estimator import (
    Crossing,
    estimate_interval,
    estimate_rows,
    request_cost_usd,
)


def event(line_cost_tokens: int, util_bucket: int, *, model: str = "claude-opus-4-7") -> dict:
    return {
        "ts": "2026-06-16T00:00:00Z",
        "upstream": "api.anthropic.com",
        "model": model,
        "status": 200,
        "duration_ms": 10,
        "streaming": False,
        "errors": [],
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": line_cost_tokens,
            "cache_creation_1h_input_tokens": line_cost_tokens,
            "cache_read_input_tokens": 0,
        },
        "quota": {
            "five_hour_utilization": util_bucket / 100.0,
            "seven_day_utilization": util_bucket / 100.0,
        },
        "meta": {"organization_id": "org_1", "request_id": f"req_{util_bucket}"},
    }


def test_request_cost_uses_cache_ttl_buckets():
    usage = {
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_creation_input_tokens": 300,
        "cache_creation_5m_input_tokens": 200,
        "cache_creation_1h_input_tokens": 100,
        "cache_read_input_tokens": 1000,
    }
    cost = request_cost_usd("claude-opus-4-7", usage)
    weighted_input = 100 + 1.25 * 200 + 2.0 * 100 + 0.10 * 1000
    assert cost == ((5.0 * weighted_input) + (25.0 * 10)) / 1_000_000


def test_unknown_model_is_excluded():
    result = estimate_rows([event(10_000, 10, model="claude-unknown")], window="5h")
    assert result.status == "insufficient"
    assert result.exclusions[0].reason == "unknown_model"


def test_no_crossing_is_insufficient():
    rows = [event(10_000, 10), event(10_000, 10), event(10_000, 10)]
    result = estimate_rows(rows, window="5h")
    assert result.status == "insufficient"
    assert result.reason == "need_two_independent_crossings"
    assert result.crossings == []


def test_single_crossing_is_insufficient():
    rows = [event(10_000, 10), event(50_000, 11)]
    result = estimate_rows(rows, window="5h")
    assert result.status == "insufficient"
    assert result.reason == "need_two_independent_crossings"
    assert len(result.crossings) == 1


def test_pairwise_interval_narrows_capacity():
    # Opus 1h cache-write cost is $10/MTok, so 200k tokens = $2.00.
    rows = [
        event(1, 10),
        event(200_000, 11),
        event(200_000, 12),
        event(200_000, 13),
        event(200_000, 14),
    ]
    result = estimate_rows(rows, window="5h")
    assert result.status == "estimated"
    assert result.interval is not None
    assert result.interval.lo <= 2.0 <= result.interval.hi
    assert result.interval.mid == 2.0
    assert result.interval.width < max(c.width for c in result.crossings)
    assert round(result.to_json()["opus_cache_write_tokens"]["per_tick"]["midpoint"]) == 200_000


def test_multi_tick_event_does_not_pair_with_itself():
    interval, reason = estimate_interval([
        Crossing(k=10, cost_before=0.0, cost_after=10.0, line=2, multi_tick_group=2),
        Crossing(k=11, cost_before=0.0, cost_after=10.0, line=2, multi_tick_group=2),
    ])
    assert interval is None
    assert reason == "need_two_independent_crossings"


def test_multi_tick_event_pairs_with_later_crossing():
    interval, reason = estimate_interval([
        Crossing(k=10, cost_before=0.0, cost_after=4.0, line=2, multi_tick_group=2),
        Crossing(k=11, cost_before=0.0, cost_after=4.0, line=2, multi_tick_group=2),
        Crossing(k=12, cost_before=8.0, cost_after=8.0, line=3),
    ])
    assert reason == ""
    assert interval is not None
    assert interval.lo == 4.0
    assert interval.hi == 4.0


def test_utilization_reset_marks_run_contaminated():
    rows = [event(1, 80), event(10_000, 81), event(10_000, 10)]
    result = estimate_rows(rows, window="5h")
    assert result.status == "contaminated"
    assert result.reason == "utilization_reset"


def test_non_measurement_rows_are_skipped_not_aborted():
    # The dedicated proxy logs every request, including ones with no usage or no
    # quota headers (errors, non-message calls). These must be excluded and the
    # run must still produce an estimate from the real measurement points.
    junk_no_usage = {"model": "claude-opus-4-7", "upstream": "api.anthropic.com"}
    junk_no_quota = {
        "model": "claude-opus-4-7",
        "upstream": "api.anthropic.com",
        "usage": {"cache_creation_1h_input_tokens": 10},
    }
    rows = [junk_no_usage, event(1, 10), event(200_000, 11), junk_no_quota, event(200_000, 12)]
    result = estimate_rows(rows, window="5h")
    assert result.status == "estimated"
    assert result.interval is not None
    assert result.excluded_events >= 2


def test_organization_does_not_gate_measurement():
    # Org/upstream no longer participate in measurement: a row whose meta differs
    # is just another event, consumed if it carries the data the calc needs.
    other = event(200_000, 11)
    other["meta"] = {"organization_id": "org_2"}
    rows = [event(1, 10), other, event(200_000, 12)]
    result = estimate_rows(rows, window="5h")
    assert result.status == "estimated"
    assert result.interval is not None
