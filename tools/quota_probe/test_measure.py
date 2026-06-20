#!/usr/bin/env python3
from __future__ import annotations

import json

import pytest

from tools.quota_probe.estimator import Estimate
from tools.quota_probe.measure import DriveConfig, drive, reject_unusable_active_events


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
