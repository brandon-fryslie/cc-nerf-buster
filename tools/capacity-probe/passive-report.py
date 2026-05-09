#!/usr/bin/env python3
"""
Compute quota capacity estimates directly from cc-nerf-buster's usage.jsonl.

Unlike the active probe (which drives deliberate measurements), this reads
the raw event log and computes capacity from all observed traffic.  Every
request that flowed through the proxy contributes — no quota is spent.

Usage:
    uv run --with rich passive-report.py
    uv run --with rich passive-report.py --data-dir ~/.local/cc-nerf-buster
    uv run --with rich passive-report.py --since 30   # last 30 days
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

from rich.console import Console
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

# ── Pricing — must stay in sync with anthropic.go ─────────────────────────────

MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (1.00,  5.00),
    "claude-sonnet-4-6":         (3.00, 15.00),
    "claude-opus-4-6":           (5.00, 25.00),
    "claude-opus-4-7":           (5.00, 25.00),
}
CACHE_WRITE_MULT = 2.0
CACHE_READ_MULT  = 0.10

# A genuine window rollover drops util by more than this fraction.
# Concurrent out-of-order responses never drop more than a fraction of a percent.
ROLLOVER_THRESHOLD = 0.05

# ── Cost helpers ──────────────────────────────────────────────────────────────

def request_cost_usd(model: str | None, usage: dict | None) -> float | None:
    if not model or not usage:
        return None
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        return None
    ip, op = pricing
    weighted_input = (
        usage.get("input_tokens", 0)
        + CACHE_WRITE_MULT * usage.get("cache_creation_input_tokens", 0)
        + CACHE_READ_MULT  * usage.get("cache_read_input_tokens", 0)
    )
    return (ip * weighted_input + op * usage.get("output_tokens", 0)) / 1_000_000


def usd_to_ocw(usd: float) -> float:
    """USD → Opus Cache Write token equivalent.  Opus cache write = $10/MTok."""
    return usd / 10.0 * 1_000_000

# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Event:
    ts: datetime
    model: str | None
    usage: dict | None
    quota_5h: float | None
    quota_7d: float | None
    errors: list[str]


@dataclass
class Cycle:
    """One window cycle: rollover-to-rollover (or start/end of log)."""
    start_ts: datetime
    end_ts: datetime
    cost_usd: float
    util_delta: float   # total positive util movement within this cycle
    events: int


@dataclass
class WindowResult:
    window: str
    total_cost_usd: float
    total_util_delta: float     # sum of positive util changes, 0.0–1.0 scale
    events_with_util: int
    events_priced: int
    events_unpriced: int        # model present but not in pricing table
    cycles: list[Cycle]
    ts_first: datetime | None
    ts_last: datetime | None

    @property
    def capacity_usd_per_tick(self) -> float | None:
        if self.total_util_delta < 1e-9:
            return None
        # total_util_delta is in 0.0–1.0 units; 1% tick = 0.01
        return self.total_cost_usd * 0.01 / self.total_util_delta

    @property
    def capacity_ocw_per_tick(self) -> float | None:
        c = self.capacity_usd_per_tick
        return None if c is None else usd_to_ocw(c)

    @property
    def implied_full_quota_ocw(self) -> float | None:
        c = self.capacity_ocw_per_tick
        return None if c is None else c * 100


# ── Loading ───────────────────────────────────────────────────────────────────

def load_events(path: Path, since: datetime | None) -> tuple[list[Event], int]:
    events: list[Event] = []
    bad = 0
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            try:
                ts = datetime.fromisoformat(raw["ts"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                bad += 1
                continue
            if since is not None and ts < since:
                continue
            quota = raw.get("quota") or {}
            events.append(Event(
                ts=ts,
                model=raw.get("model"),
                usage=raw.get("usage"),
                quota_5h=quota.get("five_hour_utilization"),
                quota_7d=quota.get("seven_day_utilization"),
                errors=raw.get("errors") or [],
            ))
    events.sort(key=lambda e: e.ts)
    return events, bad


# ── Estimation ────────────────────────────────────────────────────────────────

def estimate_window(events: list[Event], get_util) -> WindowResult:
    """
    Walk events in chronological order accumulating cost and utilization delta.

    No bracketing: for a long-running continuous log, the startup ambiguity
    (unknown position within the first percent bucket) is negligible noise
    amortised across hundreds of crossings.
    """
    total_cost = 0.0
    total_util_delta = 0.0
    events_with_util = 0
    events_priced = 0
    events_unpriced = 0
    cycles: list[Cycle] = []
    ts_first: datetime | None = None
    ts_last: datetime | None = None

    prev_util: float | None = None

    # Current-cycle accumulators
    cy_start: datetime | None = None
    cy_cost  = 0.0
    cy_util  = 0.0
    cy_n     = 0

    def close_cycle(end_ts: datetime) -> None:
        nonlocal cy_start, cy_cost, cy_util, cy_n
        if cy_start is not None:
            cycles.append(Cycle(cy_start, end_ts, cy_cost, cy_util, cy_n))
        cy_start = end_ts
        cy_cost  = 0.0
        cy_util  = 0.0
        cy_n     = 0

    for ev in events:
        cost = request_cost_usd(ev.model, ev.usage)
        if cost is not None:
            total_cost += cost
            cy_cost += cost
            events_priced += 1
        elif ev.model is not None and ev.usage is not None:
            events_unpriced += 1

        util = get_util(ev)
        if util is None:
            continue

        events_with_util += 1
        if ts_first is None:
            ts_first = ev.ts
            cy_start = ev.ts
        ts_last = ev.ts
        cy_n += 1

        if prev_util is not None:
            diff = util - prev_util
            if diff > 1e-9:
                total_util_delta += diff
                cy_util += diff
            elif diff < -ROLLOVER_THRESHOLD:
                # genuine window rollover
                close_cycle(ev.ts)

        prev_util = util

    if ts_last is not None:
        close_cycle(ts_last)

    return WindowResult(
        window="",
        total_cost_usd=total_cost,
        total_util_delta=total_util_delta,
        events_with_util=events_with_util,
        events_priced=events_priced,
        events_unpriced=events_unpriced,
        cycles=cycles,
        ts_first=ts_first,
        ts_last=ts_last,
    )


# ── Rendering ─────────────────────────────────────────────────────────────────

def render_window_section(con: Console, r: WindowResult, window_label: str) -> None:
    cap_usd = r.capacity_usd_per_tick
    cap_ocw = r.capacity_ocw_per_tick
    full_ocw = r.implied_full_quota_ocw

    con.print(Rule(f"[bold cyan]{window_label} window[/]", characters="─"))
    con.print()

    if cap_usd is None:
        con.print("  [yellow]Insufficient data — no utilization movement observed[/]")
        con.print()
        return

    headline = Text("  ")
    headline.append(f"{int(round(cap_ocw)):,}", style="bold green")
    headline.append(" OCW tokens per 1% tick", style="bold")
    headline.append(f"   (${cap_usd:.4f} / tick)", style="dim")
    con.print(headline)

    quota_line = Text("  ")
    quota_line.append("→ implied full-window quota: ", style="dim")
    quota_line.append(f"{int(round(full_ocw)):,} OCW tokens", style="bold")
    con.print(quota_line)
    con.print()

    stats = [
        ("Cost tracked",      f"${r.total_cost_usd:,.4f}"),
        ("Util accumulated",  f"{r.total_util_delta * 100:.2f} pct-pts  ({len(r.cycles)} window cycles)"),
        ("Events with quota", f"{r.events_with_util:,}"),
        ("Events priced",     f"{r.events_priced:,}"),
    ]
    if r.events_unpriced:
        stats.append(("Unknown model", f"{r.events_unpriced:,}  (excluded from cost)"))
    if r.ts_first and r.ts_last:
        span = r.ts_last - r.ts_first
        stats.append(("Data range", f"{r.ts_first:%Y-%m-%d} → {r.ts_last:%Y-%m-%d}  ({span.days}d)"))

    lw = max(len(k) for k, _ in stats)
    for k, v in stats:
        con.print(f"  [dim]{k.ljust(lw)}[/]   {v}")
    con.print()

    # Per-cycle audit table (most recent 10)
    recent = r.cycles[-10:] if len(r.cycles) > 10 else r.cycles
    if recent:
        table = Table(show_header=True, header_style="bold dim", box=None, padding=(0, 2))
        table.add_column("cycle",     style="bright_black", justify="right")
        table.add_column("start",     style="dim")
        table.add_column("cost",      justify="right")
        table.add_column("util Δ",    justify="right")
        table.add_column("cap/tick",  justify="right")
        table.add_column("events",    justify="right")

        offset = max(0, len(r.cycles) - 10)
        for i, cy in enumerate(recent, start=offset + 1):
            cap = f"${cy.cost_usd * 0.01 / cy.util_delta:.3f}" if cy.util_delta > 1e-9 else "—"
            table.add_row(
                str(i),
                cy.start_ts.strftime("%m-%d %H:%M"),
                f"${cy.cost_usd:.3f}",
                f"{cy.util_delta * 100:.2f}%",
                cap,
                str(cy.events),
            )
        con.print("  [bold dim]Per-cycle breakdown (most recent 10):[/]")
        con.print(table)
    con.print()


def render_report(con: Console, r5h: WindowResult, r7d: WindowResult, total_events: int, bad_lines: int, data_dir: Path) -> None:
    con.print()
    con.print(Rule("[bold cyan]Passive Capacity Report[/]", characters="═"))
    con.print()

    ts_range = ""
    all_first = min(t for t in [r5h.ts_first, r7d.ts_first] if t)
    all_last  = max(t for t in [r5h.ts_last,  r7d.ts_last]  if t)
    if all_first and all_last:
        span = all_last - all_first
        ts_range = f"{all_first:%Y-%m-%d} → {all_last:%Y-%m-%d}  ({span.days}d)"

    meta = [
        ("Source",  str(data_dir / "usage.jsonl")),
        ("Events",  f"{total_events:,}" + (f"  ({bad_lines} unparseable)" if bad_lines else "")),
        ("Range",   ts_range),
    ]
    lw = max(len(k) for k, _ in meta)
    for k, v in meta:
        con.print(f"  [dim]{k.ljust(lw)}[/]   {v}")
    con.print()

    render_window_section(con, r5h, "5-hour")
    render_window_section(con, r7d, "7-day")


# ── Main ──────────────────────────────────────────────────────────────────────

def default_data_dir() -> Path:
    xdg = Path(os.environ["XDG_DATA_HOME"]) if "XDG_DATA_HOME" in __import__("os").environ else None
    if xdg:
        return xdg / "cc-nerf-buster"
    return Path.home() / ".local" / "cc-nerf-buster"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", type=Path, default=None)
    p.add_argument("--since", type=int, default=None, metavar="DAYS",
                   help="Only include events from the last N days")
    return p.parse_args()


def main() -> None:
    import os
    args = parse_args()
    data_dir = args.data_dir or default_data_dir()
    jsonl_path = data_dir / "usage.jsonl"

    if not jsonl_path.exists():
        print(f"error: {jsonl_path} not found", file=sys.stderr)
        raise SystemExit(1)

    since: datetime | None = None
    if args.since is not None:
        since = datetime.now(timezone.utc) - timedelta(days=args.since)

    events, bad_lines = load_events(jsonl_path, since)
    if not events:
        print("error: no events loaded", file=sys.stderr)
        raise SystemExit(1)

    r5h = estimate_window(events, lambda e: e.quota_5h)
    r5h.window = "5h"
    r7d = estimate_window(events, lambda e: e.quota_7d)
    r7d.window = "7d"

    con = Console(stderr=False)
    render_report(con, r5h, r7d, len(events), bad_lines, data_dir)


if __name__ == "__main__":
    main()
