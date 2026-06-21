#!/usr/bin/env python3
from __future__ import annotations

import json

import pytest

from tools.quota_probe import measure
from tools.quota_probe.estimator import Estimate
from tools.quota_probe.measure import DriveConfig, drive, reject_unusable_active_events


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
    assert saved["weighted_usd_per_tick"]["relative_width"] <= 0.20


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

    # The estimate's interval tightens as crossings accumulate.
    widths = [r["relative_width"] for r in rows if r["relative_width"] is not None]
    assert len(widths) >= 2
    assert widths[-1] < widths[0]
    assert widths[-1] <= 0.10


def test_active_run_rejects_unusable_events():
    estimate = Estimate(
        schema_version=1,
        status="insufficient",
        reason="need_two_independent_crossings",
        window="5h",
        loaded_events=3,
        priced_events=0,
        excluded_events=3,
        measured_cost_usd=0.0,
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
