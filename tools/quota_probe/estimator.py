#!/usr/bin/env python3
"""Event-sourced Claude Code quota estimator.

The estimator starts from the observable boundary: cc-nerf-buster's
`APIEvent` JSONL rows. It does not use the live UI, Prometheus gauges, or the
proxy lifetime accumulator as an input to the answer.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


WINDOW_UTIL_FIELD = {
    "5h": "five_hour_utilization",
    "7d": "seven_day_utilization",
}


@dataclass(frozen=True)
class Interval:
    lo: float
    hi: float

    def __post_init__(self) -> None:
        if not (math.isfinite(self.lo) and math.isfinite(self.hi)):
            raise ValueError(f"non-finite interval [{self.lo}, {self.hi}]")
        if self.lo > self.hi:
            raise ValueError(f"invalid interval [{self.lo}, {self.hi}]")

    @property
    def mid(self) -> float:
        return (self.lo + self.hi) / 2.0

    @property
    def width(self) -> float:
        return self.hi - self.lo

    @property
    def relative_width(self) -> float:
        if self.mid <= 0:
            return math.inf
        return self.width / self.mid

    def intersect(self, other: "Interval") -> "Interval":
        lo = max(self.lo, other.lo)
        hi = min(self.hi, other.hi)
        if lo > hi:
            raise ValueError(f"disjoint intervals [{self.lo}, {self.hi}] and [{other.lo}, {other.hi}]")
        return Interval(lo, hi)

    def to_json(self) -> dict[str, float]:
        return {
            "low": self.lo,
            "midpoint": self.mid,
            "high": self.hi,
            "width": self.width,
            "relative_width": self.relative_width,
        }


@dataclass(frozen=True)
class Pricing:
    input_per_mtok: float
    output_per_mtok: float


# [LAW:one-source-of-truth] This table mirrors anthropic.go RequestCost for
# the fresh estimator boundary. The script fails closed on unknown models so a
# pricing drift cannot silently become a quota estimate.
MODEL_PRICING = {
    "claude-haiku-4-5-20251001": Pricing(1.00, 5.00),
    "claude-sonnet-4-6": Pricing(3.00, 15.00),
    "claude-opus-4-6": Pricing(5.00, 25.00),
    "claude-opus-4-7": Pricing(5.00, 25.00),
}

CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.00
CACHE_READ_MULTIPLIER = 0.10
OPUS_CACHE_WRITE_USD_PER_MTOK = 10.0


@dataclass(frozen=True)
class Exclusion:
    line: int
    reason: str
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Observation:
    line: int
    ts: str
    cost_usd: float
    util: float
    bucket: int


@dataclass(frozen=True)
class Crossing:
    k: int
    cost_before: float
    cost_after: float
    line: int
    multi_tick_group: int = 0

    @property
    def width(self) -> float:
        return self.cost_after - self.cost_before

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Estimate:
    schema_version: int
    status: str
    reason: str
    window: str
    loaded_events: int
    priced_events: int
    excluded_events: int
    measured_cost_usd: float
    crossings: list[Crossing]
    interval: Interval | None
    exclusions: list[Exclusion]

    def to_json(self) -> dict[str, Any]:
        interval_json = None if self.interval is None else self.interval.to_json()
        full_quota_json = None
        opus_cache_write_json = None
        if self.interval is not None:
            full = Interval(self.interval.lo * 100.0, self.interval.hi * 100.0)
            full_quota_json = full.to_json()
            opus_cache_write_json = {
                "per_tick": tokens_from_usd_interval(self.interval, OPUS_CACHE_WRITE_USD_PER_MTOK).to_json(),
                "full_quota": tokens_from_usd_interval(full, OPUS_CACHE_WRITE_USD_PER_MTOK).to_json(),
            }
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "reason": self.reason,
            "window": self.window,
            "events": {
                "loaded": self.loaded_events,
                "priced": self.priced_events,
                "excluded": self.excluded_events,
            },
            "measured_cost_usd": self.measured_cost_usd,
            "crossing_count": len(self.crossings),
            "crossings": [c.to_json() for c in self.crossings],
            "weighted_usd_per_tick": interval_json,
            "weighted_usd_full_quota": full_quota_json,
            "opus_cache_write_tokens": opus_cache_write_json,
            "exclusions": [e.to_json() for e in self.exclusions],
        }


def tokens_from_usd_interval(interval: Interval, usd_per_mtok: float) -> Interval:
    scale = 1_000_000.0 / usd_per_mtok
    return Interval(interval.lo * scale, interval.hi * scale)


def load_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[Exclusion]]:
    rows: list[dict[str, Any]] = []
    exclusions: list[Exclusion] = []
    with path.open() as f:
        for line_num, line in enumerate(f, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError as exc:
                exclusions.append(Exclusion(line_num, "invalid_json", str(exc)))
                continue
            if not isinstance(parsed, dict):
                exclusions.append(Exclusion(line_num, "non_object_json"))
                continue
            rows.append(parsed)
    return rows, exclusions


def request_cost_usd(model: str, usage: dict[str, Any]) -> float | None:
    pricing = MODEL_PRICING.get(model.strip())
    if pricing is None:
        return None
    input_tokens = number(usage.get("input_tokens"))
    output_tokens = number(usage.get("output_tokens"))
    cache_create = number(usage.get("cache_creation_input_tokens"))
    cache_read = number(usage.get("cache_read_input_tokens"))
    cache_5m = number(usage.get("cache_creation_5m_input_tokens"))
    cache_1h = number(usage.get("cache_creation_1h_input_tokens"))
    if cache_5m == 0 and cache_1h == 0:
        cache_1h = cache_create
    weighted_input = (
        input_tokens
        + CACHE_WRITE_5M_MULTIPLIER * cache_5m
        + CACHE_WRITE_1H_MULTIPLIER * cache_1h
        + CACHE_READ_MULTIPLIER * cache_read
    )
    return (pricing.input_per_mtok * weighted_input + pricing.output_per_mtok * output_tokens) / 1_000_000.0


def number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return 0.0


def utilization_bucket(util: float) -> int:
    return int(util * 100.0 + 1e-9)


def build_observations(
    rows: list[dict[str, Any]],
    *,
    window: str,
) -> tuple[list[Observation], list[Exclusion]]:
    # [LAW:one-source-of-truth] The run's usage log, produced by a proxy only the
    # probe used, is the single dataset. A row is a measurement point when it
    # carries a window utilization reading and a priceable cost; every other row
    # is recorded as an exclusion and skipped, never a reason to abort.
    util_field = WINDOW_UTIL_FIELD[window]
    observations: list[Observation] = []
    exclusions: list[Exclusion] = []
    for line_num, row in enumerate(rows, 1):
        model = row.get("model")
        if not isinstance(model, str) or not model:
            exclusions.append(Exclusion(line_num, "missing_model"))
            continue
        usage = row.get("usage")
        if not isinstance(usage, dict):
            exclusions.append(Exclusion(line_num, "missing_usage"))
            continue
        cost = request_cost_usd(model, usage)
        if cost is None:
            exclusions.append(Exclusion(line_num, "unknown_model", model))
            continue
        quota = row.get("quota")
        if not isinstance(quota, dict):
            exclusions.append(Exclusion(line_num, "missing_quota"))
            continue
        util = quota.get(util_field)
        if not isinstance(util, (int, float)) or not math.isfinite(float(util)):
            exclusions.append(Exclusion(line_num, f"missing_{window}_utilization"))
            continue
        util_float = float(util)
        if util_float < 0 or util_float > 1:
            exclusions.append(Exclusion(line_num, "invalid_utilization", str(util_float)))
            continue
        observations.append(
            Observation(
                line=line_num,
                ts=str(row.get("ts") or ""),
                cost_usd=cost,
                util=util_float,
                bucket=utilization_bucket(util_float),
            )
        )
    return observations, exclusions


def build_crossings(observations: list[Observation]) -> tuple[list[Crossing], float, str]:
    if len(observations) < 2:
        return [], 0.0, "need_at_least_two_observations"
    crossings: list[Crossing] = []
    previous_bucket = observations[0].bucket
    measured_cost = 0.0
    for obs in observations[1:]:
        cost_before = measured_cost
        measured_cost += obs.cost_usd
        cost_after = measured_cost
        if obs.bucket < previous_bucket:
            return crossings, measured_cost, "utilization_reset"
        if obs.bucket > previous_bucket:
            crossed = obs.bucket - previous_bucket
            group = obs.line if crossed > 1 else 0
            for k in range(previous_bucket + 1, obs.bucket + 1):
                crossings.append(
                    Crossing(
                        k=k,
                        cost_before=cost_before,
                        cost_after=cost_after,
                        line=obs.line,
                        multi_tick_group=group,
                    )
                )
        previous_bucket = obs.bucket
    return crossings, measured_cost, ""


def estimate_interval(crossings: list[Crossing]) -> tuple[Interval | None, str]:
    interval: Interval | None = None
    usable_pairs = 0
    for i, a in enumerate(crossings):
        for b in crossings[i + 1:]:
            if b.k <= a.k:
                continue
            if a.multi_tick_group != 0 and a.multi_tick_group == b.multi_tick_group:
                continue
            dk = b.k - a.k
            pair = Interval(
                max(0.0, (b.cost_before - a.cost_after) / dk),
                (b.cost_after - a.cost_before) / dk,
            )
            usable_pairs += 1
            try:
                interval = pair if interval is None else interval.intersect(pair)
            except ValueError as exc:
                return None, f"disjoint_constraints: {exc}"
    if usable_pairs == 0:
        return None, "need_two_independent_crossings"
    return interval, ""


def estimate_rows(
    rows: list[dict[str, Any]],
    *,
    window: str,
    parse_exclusions: list[Exclusion] | None = None,
) -> Estimate:
    if window not in WINDOW_UTIL_FIELD:
        raise ValueError(f"unsupported window {window!r}")
    base_exclusions = list(parse_exclusions or [])
    observations, row_exclusions = build_observations(rows, window=window)
    exclusions = base_exclusions + row_exclusions
    crossings, measured_cost, crossing_reason = build_crossings(observations)
    if crossing_reason == "utilization_reset":
        return Estimate(
            schema_version=1,
            status="contaminated",
            reason=crossing_reason,
            window=window,
            loaded_events=len(rows),
            priced_events=len(observations),
            excluded_events=len(exclusions),
            measured_cost_usd=measured_cost,
            crossings=crossings,
            interval=None,
            exclusions=exclusions,
        )
    interval, estimate_reason = estimate_interval(crossings)
    status = "estimated" if interval is not None else "insufficient"
    reason = "" if interval is not None else (estimate_reason or crossing_reason)
    return Estimate(
        schema_version=1,
        status=status,
        reason=reason,
        window=window,
        loaded_events=len(rows),
        priced_events=len(observations),
        excluded_events=len(exclusions),
        measured_cost_usd=measured_cost,
        crossings=crossings,
        interval=interval,
        exclusions=exclusions,
    )


def estimate_usage_log(path: Path, *, window: str) -> Estimate:
    rows, parse_exclusions = load_jsonl(path)
    return estimate_rows(rows, window=window, parse_exclusions=parse_exclusions)

