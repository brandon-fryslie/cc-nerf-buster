"""Position-constraint observations and intervals for capacity estimation.

A `Crossing` is the canonical observation type produced by the probe each time
the API's util% advances by one integer percent: it records the absolute tick
number `k`, the cumulative probe-sent input-equivalent tokens immediately
before the crossing iter (`Y_before`), and the same quantity immediately after
(`Y_after`). Each crossing is a labeled position constraint:

    k * C - Q0 in [Y_before, Y_after]

where `C` is tokens-per-tick (the value to estimate) and `Q0` is the (unknown)
quota usage at probe start. `Q0` is eliminated by pairing two crossings.

An `Interval` is `[lo, hi]` with an `intersect` that only ever narrows. Disjoint
intersections raise; the probe should never observe contradictory constraints
on `C` unless something is fundamentally wrong (prior bad, system non-linear,
or recording bug). Failing loudly is the point.

# // [LAW:types-are-the-program] These two types encode the strongest theorem
# the probe can state about a crossing observation: a labeled position
# constraint that, paired with any other, eliminates Q0 and bounds C. Every
# downstream estimator and UI reads from this shape; no other arithmetic is
# admitted on the raw observation.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import json
import math


# Live-recorded position-constraint stream. Distinct from `crossings.jsonl`,
# which is a USD-cost-bracket file derived post-hoc by `report.py`.
POSITION_CONSTRAINTS_FILENAME = "position-constraints.jsonl"


@dataclass(frozen=True)
class Interval:
    lo: float
    hi: float

    def __post_init__(self) -> None:
        if not (self.lo <= self.hi):
            raise ValueError(f"Interval requires lo <= hi, got [{self.lo}, {self.hi}]")

    @property
    def width(self) -> float:
        return self.hi - self.lo

    @property
    def mid(self) -> float:
        return (self.lo + self.hi) / 2.0

    def intersect(self, other: "Interval") -> "Interval":
        # // [LAW:types-are-the-program] disjoint intersection is unrepresentable as
        # a narrowed interval; it indicates contradictory observations and must
        # surface as a hard failure rather than silently producing an empty range.
        new_lo = max(self.lo, other.lo)
        new_hi = min(self.hi, other.hi)
        if new_lo > new_hi:
            raise ValueError(
                f"Disjoint intervals: self={self}, other={other} "
                f"(intersection would be [{new_lo}, {new_hi}])"
            )
        return Interval(new_lo, new_hi)


@dataclass(frozen=True)
class Crossing:
    """One observed integer-percent crossing.

    `k` is the absolute util%-after value reported by the API (1..100), not a
    tick-since-baseline count. `Y_before` and `Y_after` are cumulative probe
    input-equivalent tokens at the close of the iter immediately before, and
    immediately after, the crossing iter ran. `iter_num` is the probe iter
    that produced the crossing (for backreference into iterations.jsonl).

    `multi_tick_group` is nonzero when a single iter crossed more than one
    integer-percent boundary; all crossings produced by that iter share a
    group id (and share `Y_before`/`Y_after`). The estimator treats each k
    in the group as a separate constraint but knows their brackets are not
    independent.
    """

    k: int
    Y_before: float
    Y_after: float
    iter_num: int
    multi_tick_group: int = 0

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "Crossing":
        return cls(
            k=int(d["k"]),
            Y_before=float(d["Y_before"]),
            Y_after=float(d["Y_after"]),
            iter_num=int(d["iter_num"]),
            multi_tick_group=int(d.get("multi_tick_group", 0)),
        )


def append_crossings(
    run_dir: Path,
    *,
    util_pct_pre: int,
    util_pct_post: int,
    Y_before: float,
    Y_after: float,
    iter_num: int,
) -> list[Crossing]:
    """Record one Crossing per integer-percent boundary crossed by this iter.

    If a single iter crosses N>1 boundaries (rare; happens only with iters
    larger than one tick of quota), N Crossings are recorded sharing the same
    Y bracket and `multi_tick_group` id (= iter_num).
    """
    crossed = util_pct_post - util_pct_pre
    if crossed <= 0:
        return []
    group_id = iter_num if crossed > 1 else 0
    out: list[Crossing] = []
    path = run_dir / POSITION_CONSTRAINTS_FILENAME
    with path.open("a") as f:
        for k in range(util_pct_pre + 1, util_pct_post + 1):
            c = Crossing(
                k=k,
                Y_before=float(Y_before),
                Y_after=float(Y_after),
                iter_num=int(iter_num),
                multi_tick_group=group_id,
            )
            f.write(json.dumps(c.to_json()) + "\n")
            out.append(c)
    return out


def load_crossings(run_dir: Path) -> list[Crossing]:
    """Load all Crossings recorded for a run, in observation order."""
    path = run_dir / POSITION_CONSTRAINTS_FILENAME
    if not path.exists():
        return []
    out: list[Crossing] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(Crossing.from_json(json.loads(line)))
    return out


def derive_crossings_from_iterations(run_dir: Path) -> list[Crossing]:
    """Reconstruct Crossings post-hoc from iterations.jsonl + snapshots.jsonl.

    Used by the validation script to compute the new estimator against runs
    that pre-date live position-constraint recording. Walks iterations in
    order; for each iter that advances util%, emits one Crossing per integer
    boundary.

    Cumulative Y is reconstructed by summing `input_equivalent_tokens` across
    iterations (matches probe.py's `total_units` accumulator). The util%-pre
    and util%-post for an iter come from `snapshots.jsonl` rows keyed by the
    iter's `<n>-after` label.
    """
    iters_path = run_dir / "iterations.jsonl"
    snaps_path = run_dir / "snapshots.jsonl"
    if not iters_path.exists() or not snaps_path.exists():
        return []

    # // [LAW:one-source-of-truth] snapshots.jsonl is the canonical parsed
    # record of util% at each metric fetch. Reconstruct util_pre/post from
    # it rather than re-deriving from cumulative tokens (which would require
    # us to already know C — the very value we're trying to estimate).
    manifest = json.loads((run_dir / "manifest.json").read_text())
    window = manifest["window"]
    util_key = f"util_{window}"

    snaps_by_label: dict[str, dict] = {}
    with snaps_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            snaps_by_label[d["label"]] = d

    baseline = snaps_by_label.get("000-baseline")
    if baseline is None:
        return []
    baseline_pct = int(float(baseline[util_key]) * 100 + 1e-9)

    out: list[Crossing] = []
    cumulative_Y = 0.0
    util_pct_pre = baseline_pct
    with iters_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            it = json.loads(line)
            iter_units = float(it["input_equivalent_tokens"])
            iter_num = int(it["iter"])
            Y_before = cumulative_Y
            cumulative_Y += iter_units
            Y_after = cumulative_Y
            snap = snaps_by_label.get(f"{iter_num:03d}-after")
            if snap is None:
                continue
            util_pct_post = int(float(snap[util_key]) * 100 + 1e-9)
            if util_pct_post > util_pct_pre:
                crossed = util_pct_post - util_pct_pre
                group_id = iter_num if crossed > 1 else 0
                for k in range(util_pct_pre + 1, util_pct_post + 1):
                    out.append(Crossing(
                        k=k,
                        Y_before=Y_before,
                        Y_after=Y_after,
                        iter_num=iter_num,
                        multi_tick_group=group_id,
                    ))
            util_pct_pre = util_pct_post
    return out


def estimate_C(crossings: list[Crossing], prior: Interval) -> Interval:
    """Intersect pairwise constraints on C across all observed crossings.

    For each ordered pair (a, b) with k_a < k_b:

        (k_b - k_a) * C in [Y_b_before - Y_a_after, Y_b_after - Y_a_before]

    The returned interval is the intersection of `prior` with every such pair
    constraint. Multi-tick-group crossings (multiple k's sharing a Y bracket)
    are treated as independent constraints; this is correct because each `k`
    gives a separate position constraint, even though they share a bracket.

    # // [LAW:types-are-the-program] the estimator is forced by the type:
    # `Crossing` admits exactly the pairwise-subtraction operation that yields
    # a bound on C. There is no other arithmetic; no averaging of differences,
    # no special-case "leading bracket" exclusion. Every Crossing participates.
    """
    if not crossings:
        return prior
    result = prior
    for i, a in enumerate(crossings):
        for b in crossings[i + 1:]:
            if b.k <= a.k:
                continue
            dk = b.k - a.k
            pair_lo = (b.Y_before - a.Y_after) / dk
            pair_hi = (b.Y_after - a.Y_before) / dk
            if not math.isfinite(pair_lo) or not math.isfinite(pair_hi):
                continue
            if pair_lo > pair_hi:
                # Degenerate pair (shouldn't happen with consistent data).
                continue
            result = result.intersect(Interval(pair_lo, pair_hi))
    return result
