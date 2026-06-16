#!/usr/bin/env python3
from __future__ import annotations

import json

from tools.quota_probe.measure import DriveConfig, drive


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

