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
