#!/usr/bin/env python3
from __future__ import annotations

import json

import pytest

from tools.quota_probe import measure
from tools.quota_probe.estimator import Estimate, Interval
from tools.quota_probe.measure import DriveConfig, drive, reject_unusable_active_events


def _estimate_with_cost(measured_cost: float) -> Estimate:
    return Estimate(
        schema_version=2,
        status="estimated",
        reason="",
        window="5h",
        loaded_events=2,
        priced_events=2,
        excluded_events=0,
        measured_cost=measured_cost,
        crossings=[],
        interval=Interval(2.0, 3.0),
        exclusions=[],
    )


def _cfg(tmp_path):
    return DriveConfig(
        window="5h",
        run_dir=tmp_path,
        model="claude-opus-4-7",
        target_relative_width=0.2,
        max_iters=1,
        dry_run=False,
    )


def test_dry_run_drive_writes_replayable_artifacts(tmp_path):
    cfg = DriveConfig(
        window="5h",
        run_dir=tmp_path,
        model="claude-opus-4-7",
        target_relative_width=0.20,
        max_iters=40,
        dry_run=True,
    )
    estimate = drive(cfg)
    assert estimate.status == "estimated"
    assert (tmp_path / "manifest.json").exists()
    assert (tmp_path / "usage.jsonl").exists()
    assert (tmp_path / "fresh-bounds.json").exists()
    assert (tmp_path / "fresh-report.md").exists()
    saved = json.loads((tmp_path / "fresh-bounds.json").read_text())
    assert saved["status"] == "estimated"
    assert saved["cost_per_tick"]["relative_width"] <= 0.20


def test_build_prompt_emits_whole_blocks():
    # Stage A contract: the prompt is the header plus exactly `blocks` fixed paragraphs,
    # never truncated — that is what makes input_tokens a deterministic line in blocks.
    assert measure.build_prompt(0).count(measure.PROMPT_PARAGRAPH) == 0
    assert measure.build_prompt(3).count(measure.PROMPT_PARAGRAPH) == 3
    assert measure.PROMPT_HEADER in measure.build_prompt(2)


def test_fit_block_line_recovers_constants():
    a_true, b_true = 25.0, 72.0
    samples = [(k, int(a_true + k * b_true)) for k in (0, 10, 250)]
    a, b = measure.fit_block_line(samples)
    assert abs(b - b_true) < 0.5
    assert abs(a - a_true) < 1.0
    # one point cannot separate intercept from slope
    assert measure.fit_block_line([(5, 360)]) is None


def test_recalibrate_flags_cache_write_inflation():
    # b=72 -> no-cache prediction is 72 * opus input cost. recalibrate stays pure:
    # it returns the warning text, never prints it. [LAW:effects-at-boundaries]
    seed = measure.seed_actuator("claude-opus-4-7")
    samples = [(0, 25), (100, 25 + 100 * 72)]
    predicted = 72 * measure.model_input_cost_per_token("claude-opus-4-7")

    # observed cost/block well above predicted * 1.5 -> warning
    inflated = predicted * 100 * 3.0  # total_blocks=100
    actuator, warning = measure.recalibrate(seed, samples, _estimate_with_cost(inflated), 100, "claude-opus-4-7")
    assert abs(actuator.tokens_per_block - 72) < 0.5
    assert warning is not None and "cache-write" in warning

    # observed cost/block matching the prediction -> no warning
    onprice = predicted * 100
    _, clean = measure.recalibrate(seed, samples, _estimate_with_cost(onprice), 100, "claude-opus-4-7")
    assert clean is None


def test_token_actuator_converges_and_tracks(tmp_path):
    cfg = DriveConfig(
        window="5h",
        run_dir=tmp_path,
        model="claude-opus-4-7",
        target_relative_width=0.005,
        max_iters=60,
        dry_run=True,
    )
    drive(cfg)
    rows = [
        json.loads(line)
        for line in (tmp_path / "driver-iterations.jsonl").read_text().splitlines()
        if line.strip() and "tokens_per_block" in line
    ]
    assert rows, "no calibration rows were logged"

    # Stage A converged to the synthetic's TRUE block size (72), which differs from the
    # actuator seed (57). A pass therefore proves the closed loop, not shared constants.
    assert abs(rows[-1]["tokens_per_block"] - measure.DRYRUN_TRUE_TOKENS_PER_BLOCK) < 0.5
    assert measure.DEFAULT_TOKENS_PER_BLOCK != measure.DRYRUN_TRUE_TOKENS_PER_BLOCK

    # Post-bootstrap, the prompt the actuator builds lands on its input-token target.
    for r in rows[2:]:
        assert abs(r["observed_input_tokens"] - r["input_tokens_target"]) <= 80

    # The estimate's interval tightens as crossings accumulate. The bulk-then-bisect sizer
    # reaches a width the old uniform-step sizer could not (~0.05 on the same 60-call budget):
    # 0.01 is a bar only the convergent sizer clears, locking in the >=5x improvement.
    widths = [r["relative_width"] for r in rows if r["relative_width"] is not None]
    assert len(widths) >= 2
    assert widths[-1] < widths[0]
    assert widths[-1] <= 0.01

    # Per-crossing brackets shrink visibly across ticks: the bisect phase plants a small step
    # at the boundary, so later crossings are far tighter than the bootstrap/anchor crossings.
    crossings = json.loads((tmp_path / "fresh-bounds.json").read_text())["crossings"]
    bracket_widths = [c["cost_after"] - c["cost_before"] for c in crossings]
    assert len(bracket_widths) >= 4
    assert min(bracket_widths[2:]) < bracket_widths[0] / 3.0


def _estimate(measured_cost, crossings, interval):
    return Estimate(
        schema_version=2,
        status="estimated",
        reason="",
        window="5h",
        loaded_events=len(crossings) + 2,
        priced_events=len(crossings) + 2,
        excluded_events=0,
        measured_cost=measured_cost,
        crossings=crossings,
        interval=interval,
        exclusions=[],
    )


def test_boundary_window_bootstrap_is_one_tick_from_now():
    # No crossing yet: Q0 is unbracketed, so the next boundary is only known to lie within
    # one prior tick of now. The window is that full-tick band starting at now.
    est = _estimate(7.0, crossings=[], interval=None)
    window = measure.boundary_window(est, prior_tick=2.75)
    assert window.lo == pytest.approx(7.0)
    assert window.hi == pytest.approx(7.0 + 2.75)


def test_boundary_window_anchors_on_last_crossing_plus_interval():
    from tools.quota_probe.estimator import Crossing
    crossings = [Crossing(k=10, cost_before=4.0, cost_after=4.3, line=5)]
    est = _estimate(4.3, crossings=crossings, interval=Interval(2.0, 2.5))
    window = measure.boundary_window(est, prior_tick=2.75)
    # [cost_last_before + C_lo, cost_last_after + C_hi]
    assert window.lo == pytest.approx(4.0 + 2.0)
    assert window.hi == pytest.approx(4.3 + 2.5)


def test_boundary_window_one_crossing_no_interval_stays_bootstrap():
    # A single crossing cannot pair into an interval, so there is no trustworthy C: anchoring
    # on it would predict the next boundary one *prior* tick out (the prior runs ~3x the real
    # tick) and bulk would blow through the true boundary. The window stays the bootstrap band.
    from tools.quota_probe.estimator import Crossing
    crossings = [Crossing(k=10, cost_before=4.0, cost_after=4.3, line=5)]
    est = _estimate(4.3, crossings=crossings, interval=None)
    window = measure.boundary_window(est, prior_tick=2.75)
    assert window.lo == pytest.approx(4.3)
    assert window.hi == pytest.approx(4.3 + 2.75)


def test_sizer_bulk_dominates_far_below_window():
    from tools.quota_probe.estimator import Crossing
    crossings = [Crossing(k=10, cost_before=0.0, cost_after=0.1, line=5)]
    # now far below a narrow window [10.0, 10.6]; bulk = 0.9*(10.0-0.1) beats bisect = (10.6-0.1)/2.
    est = _estimate(0.1, crossings=crossings, interval=Interval(10.0, 10.5))
    actuator = measure.Actuator(header_tokens=25.0, tokens_per_block=72.0, cost_per_block=72.0 * 5e-6)
    target = measure.target_input_tokens_for_estimate(est, actuator)
    expected_cost = measure.BULK_UNDERSHOOT * (10.0 - 0.1)
    expected = 25.0 + (expected_cost / actuator.cost_per_block) * 72.0
    assert target == pytest.approx(min(measure.MAX_PROMPT_TOKENS, expected), rel=1e-6)


def test_sizer_coarse_resolution_before_first_crossing():
    # No crossing yet: window.lo == now so bulk == 0, and the step is one COARSE resolution of
    # the prior tick — a fast, wide search for the first boundary that only has to anchor Q0.
    est = _estimate(7.0, crossings=[], interval=None)
    actuator = measure.Actuator(header_tokens=25.0, tokens_per_block=72.0, cost_per_block=72.0 * 5e-6)
    target = measure.target_input_tokens_for_estimate(est, actuator)
    expected_cost = measure.COARSE_TICK_FRACTION * measure.DEFAULT_TICK_COST[est.window]
    expected = 25.0 + (expected_cost / actuator.cost_per_block) * 72.0
    assert target == pytest.approx(min(measure.MAX_PROMPT_TOKENS, expected), rel=1e-6)


def test_sizer_coarse_resolution_with_one_crossing_no_interval():
    # One crossing but no interval yet: still a coarse search (bulk == 0 on the bootstrap
    # window), so the next boundary is found fast rather than bulk-predicted off the bad prior.
    from tools.quota_probe.estimator import Crossing
    crossings = [Crossing(k=10, cost_before=4.0, cost_after=4.3, line=5)]
    est = _estimate(4.3, crossings=crossings, interval=None)
    actuator = measure.Actuator(header_tokens=25.0, tokens_per_block=72.0, cost_per_block=72.0 * 5e-6)
    target = measure.target_input_tokens_for_estimate(est, actuator)
    expected_cost = measure.COARSE_TICK_FRACTION * measure.DEFAULT_TICK_COST[est.window]
    expected = int(25.0 + (expected_cost / actuator.cost_per_block) * 72.0)
    assert target == max(measure.MIN_PROMPT_TOKENS, min(measure.MAX_PROMPT_TOKENS, expected))


def test_sizer_fine_resolution_inside_window():
    from tools.quota_probe.estimator import Crossing
    crossings = [Crossing(k=10, cost_before=4.0, cost_after=4.2, line=5)]
    # now is inside the window [4.0+2.0, 4.2+2.5] = [6.0, 6.7]; bulk goes negative, so the step
    # is one FINE resolution of the live tick — a fixed bracket independent of the window width.
    est = _estimate(6.3, crossings=crossings, interval=Interval(2.0, 2.5))
    actuator = measure.Actuator(header_tokens=25.0, tokens_per_block=72.0, cost_per_block=72.0 * 5e-6)
    target = measure.target_input_tokens_for_estimate(est, actuator)
    expected_cost = measure.FINE_TICK_FRACTION * Interval(2.0, 2.5).mid
    expected = int(25.0 + (expected_cost / actuator.cost_per_block) * 72.0)
    assert target == max(measure.MIN_PROMPT_TOKENS, min(measure.MAX_PROMPT_TOKENS, expected))


def test_sizer_fine_step_past_window_does_not_stall_or_widen():
    from tools.quota_probe.estimator import Crossing
    crossings = [Crossing(k=10, cost_before=4.0, cost_after=4.2, line=5)]
    # now beyond window.hi (prediction undershot): bulk goes deeply negative, so the step stays
    # one bounded FINE resolution — the loop keeps advancing in tight steps rather than stalling
    # at MIN or blowing the bracket wide. [LAW:no-silent-failure]
    est = _estimate(99.0, crossings=crossings, interval=Interval(2.0, 2.5))
    actuator = measure.Actuator(header_tokens=25.0, tokens_per_block=72.0, cost_per_block=72.0 * 5e-6)
    target = measure.target_input_tokens_for_estimate(est, actuator)
    expected_cost = measure.FINE_TICK_FRACTION * Interval(2.0, 2.5).mid
    expected = int(25.0 + (expected_cost / actuator.cost_per_block) * 72.0)
    assert target == max(measure.MIN_PROMPT_TOKENS, min(measure.MAX_PROMPT_TOKENS, expected))
    assert target > measure.MIN_PROMPT_TOKENS


def test_claude_env_disables_nonessential_model_calls(monkeypatch, tmp_path):
    # Without this, each `claude -p` fires a second (haiku title-generation) model call that
    # re-sends the prompt and is billed against the same quota, contaminating measured cost.
    # The probe's invocation env MUST suppress it so one served opus event maps to one call.
    # The tokens are the ones the CLI binary actually reads; verified on real traffic. Both are
    # required and masked each other: the title call re-sent the prompt as input_tokens, while
    # prompt caching shunted it into cache_creation. Only with both off does input_tokens equal
    # the prompt the actuator built.
    monkeypatch.setenv("PROXY_URL", "http://localhost:9")
    env = measure.claude_env(tmp_path)
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert env["DISABLE_PROMPT_CACHING"] == "1"


def test_active_run_rejects_unusable_events():
    estimate = Estimate(
        schema_version=1,
        status="insufficient",
        reason="need_two_independent_crossings",
        window="5h",
        loaded_events=3,
        priced_events=0,
        excluded_events=3,
        measured_cost=0.0,
        crossings=[],
        interval=None,
        exclusions=[],
    )
    with pytest.raises(SystemExit):
        reject_unusable_active_events(estimate)


def test_transient_failures_are_retried_then_succeed(monkeypatch, tmp_path):
    calls = {"n": 0}

    def fake_run_claude(prompt, *, cfg, iter_num, attempt):
        calls["n"] += 1
        return calls["n"] >= 3  # fail the first two attempts, succeed on the third

    monkeypatch.setattr(measure, "run_claude", fake_run_claude)
    monkeypatch.setattr(measure, "wait_for_usage_event", lambda path, prev: True)
    monkeypatch.setattr(measure.time, "sleep", lambda _s: None)

    assert measure.attempt_claude_call(
        "p", cfg=_cfg(tmp_path), iter_num=1, usage_path=tmp_path / "usage.jsonl"
    ) is True
    assert calls["n"] == 3


def test_call_gives_up_after_max_attempts(monkeypatch, tmp_path):
    calls = {"n": 0}

    def always_fail(prompt, *, cfg, iter_num, attempt):
        calls["n"] += 1
        return False

    monkeypatch.setattr(measure, "run_claude", always_fail)
    monkeypatch.setattr(measure.time, "sleep", lambda _s: None)

    assert measure.attempt_claude_call(
        "p", cfg=_cfg(tmp_path), iter_num=1, usage_path=tmp_path / "usage.jsonl"
    ) is False
    assert calls["n"] == measure.MAX_CALL_ATTEMPTS
