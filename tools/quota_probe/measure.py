#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.quota_probe.estimator import (
    MODEL_PRICING,
    NORMALIZED_COST_SCALE,
    Estimate,
    Interval,
    estimate_usage_log,
    number,
    request_cost,
)


DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_CLAUDE_TIMEOUT_SECONDS = 180
# A probe that pushes utilization toward the limit is the workload most likely to
# be throttled, so transient failures are expected. Retry a call a few times,
# then skip the iteration; only give up entirely after many iterations in a row
# fail (the run genuinely cannot make progress).
MAX_CALL_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0
MAX_CONSECUTIVE_FAILURES = 5
# Prior per-tick size, in NORMALIZED_COST_UNIT, used only to bootstrap iter sizing
# before the live interval exists. Same order of magnitude as the measured per-tick
# cost (see README), refined away the moment a crossing is observed.
DEFAULT_TICK_COST = {
    "5h": 275_000.0,
    "7d": 1_400_000.0,
}
# The actuator steers prompt size in input tokens — the billed unit — not chars.
# The prompt is PROMPT_HEADER followed by a whole number of fixed PROMPT_PARAGRAPH
# blocks, so input_tokens = header_tokens + blocks * tokens_per_block is linear with
# stable constants (fixed text tokenizes deterministically). These are only seeds;
# both constants are refit from the input_tokens of served events. [LAW:one-source-of-truth]
DEFAULT_HEADER_TOKENS = 16.0
DEFAULT_TOKENS_PER_BLOCK = 57.0
MIN_PROMPT_TOKENS = 64
MAX_PROMPT_TOKENS = 64_000
# Fraction of the predicted-boundary lower edge a bulk step aims for, so the
# expected landing falls *before* the boundary and a single jump cannot blow past
# it into a bulk-wide crossing bracket. Token actuation (.5) removed char->cost
# slippage, so the only residual overshoot is output/cache noise — a tight
# undershoot is safe. [LAW:no-silent-failure] an overshoot is still recorded as a
# wide crossing, never discarded.
BULK_UNDERSHOOT = 0.9
# The localize step is a fixed fraction of one tick, so the crossing it produces is
# bracketed to that fraction REGARDLESS of where in the boundary window it falls — a
# forward-only (irreversible) threshold search has no logarithmic shortcut, so a
# guaranteed-tight bracket is a fixed small step, not a halving of the wide window.
# COARSE is used before any crossing anchors Q0 (the boundary is only known to within a
# full tick, so we find it fast and accept a wide bracket that only anchors Q0); FINE is
# used once a crossing exists, to bracket every subsequent boundary to ~FINE of a tick.
# These are the accuracy/speed lever: smaller FINE -> tighter bracket, more localize calls.
COARSE_TICK_FRACTION = 0.1
FINE_TICK_FRACTION = 0.02
PROMPT_HEADER = "Read the operational note and reply with exactly: ok\n\n"
PROMPT_PARAGRAPH = (
    "Operational measurement note. The request describes ordinary service "
    "activity, quota accounting, request logging, and stable capacity "
    "measurement. Reply with one short sentence confirming receipt."
)

# Dry-run only: the synthetic API uses true constants deliberately DIFFERENT from the
# actuator seeds above, so a passing dry-run proves the calibration loop converged
# rather than that the simulator was handed the actuator's own numbers. [LAW:behavior-not-structure]
DRYRUN_TRUE_HEADER_TOKENS = 25.0
DRYRUN_TRUE_TOKENS_PER_BLOCK = 72.0
# Observed cost/block diverging from the no-cache prediction by more than this
# factor means cache-write tokens are inflating cost; surfaced, never silently absorbed.
CACHE_WRITE_WARN_FACTOR = 1.5


@dataclass(frozen=True)
class DriveConfig:
    window: str
    run_dir: Path
    model: str
    target_relative_width: float
    max_iters: int
    dry_run: bool
    # When set, the run stops after observing this many quota-tick crossings
    # instead of when the interval reaches target_relative_width. One field
    # carries which precision target governs. [LAW:dataflow-not-control-flow]
    target_ticks: int | None = None
    claude_timeout_seconds: int = DEFAULT_CLAUDE_TIMEOUT_SECONDS

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["run_dir"] = str(self.run_dir)
        return d


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_ts() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def default_run_dir(dry_run: bool) -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local"))
    prefix = "dryrun-" if dry_run else ""
    return data_home / "cc-nerf-buster" / "quota-runs" / f"{prefix}{run_ts()}"


def die(message: str) -> "NoReturn":
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


@dataclass(frozen=True)
class Actuator:
    # The live model of how a prompt becomes cost, in two separately-calibrated stages:
    #   header_tokens, tokens_per_block : prompt blocks -> input_tokens (Stage A, deterministic)
    #   cost_per_block                  : prompt blocks -> measured cost (Stage B, cache-robust)
    # [LAW:one-source-of-truth] this is the single place the actuator's knowledge lives.
    header_tokens: float
    tokens_per_block: float
    cost_per_block: float


def model_input_cost_per_token(model: str) -> float:
    # Normalized cost (NORMALIZED_COST_UNIT) of one fresh input token.
    # [LAW:one-source-of-truth] weighting comes from the estimator's table, never duplicated here.
    pricing = MODEL_PRICING.get(model.strip())
    if pricing is None:
        die(f"unknown model {model!r}: no pricing to seed the actuator")
    return pricing.input_per_mtok / 1_000_000.0 * NORMALIZED_COST_SCALE


def seed_actuator(model: str) -> Actuator:
    tokens_per_block = DEFAULT_TOKENS_PER_BLOCK
    # Seed Stage B from the no-cache cost law; this is a bootstrap value, not the
    # actuation path — it is replaced by observed cost as soon as the estimate exists.
    cost_per_block = tokens_per_block * model_input_cost_per_token(model)
    return Actuator(DEFAULT_HEADER_TOKENS, tokens_per_block, cost_per_block)


def build_prompt(blocks: int) -> str:
    # Whole blocks only — a partial/truncated block would break the deterministic
    # tokenization the calibration depends on. [LAW:types-are-the-program] the block is the unit.
    parts = [PROMPT_HEADER]
    for _ in range(max(0, blocks)):
        parts.append(PROMPT_PARAGRAPH)
        parts.append("\n\n")
    return "".join(parts) + "\n"


def blocks_for_target_tokens(target_input_tokens: int, actuator: Actuator) -> int:
    target = max(MIN_PROMPT_TOKENS, min(MAX_PROMPT_TOKENS, target_input_tokens))
    need = (target - actuator.header_tokens) / max(actuator.tokens_per_block, 1e-9)
    return max(0, round(need))


def boundary_window(estimate: Estimate, prior_tick: float) -> Interval:
    # The cumulative-cost band the *next* integer util boundary (k_last + 1) falls in.
    # Anchored on the most recent Crossing (which brackets Q0) plus one tick of C:
    #     [cost_last_before + C_lo, cost_last_after + C_hi]
    # C is the live interval when it exists, else the point prior as a degenerate band.
    # Before any crossing Q0 is unbracketed, so the boundary is only known to lie within
    # one C of now — a full-tick-wide window starting at now. That is the bootstrap: not a
    # mode, just the window value when no anchor exists yet. [LAW:one-source-of-truth] C
    # lives only in estimate.interval; the Q0 anchor is only the most recent crossing.
    now = estimate.measured_cost
    if estimate.crossings and estimate.interval is not None:
        anchor = estimate.crossings[-1]
        c = estimate.interval
        return Interval(anchor.cost_before + c.lo, anchor.cost_after + c.hi)
    # No usable C yet (zero crossings, or one crossing that cannot pair into an interval):
    # the next boundary is only known to within one prior tick of now. Anchoring on a single
    # crossing would instead predict it one *prior* tick out, and the prior runs ~3x the real
    # tick — bulk would then steam-roll straight through the true next boundary, widening its
    # bracket. So we keep searching from now until a real interval exists. [LAW:no-silent-failure]
    return Interval(now, now + prior_tick)


def target_input_tokens_for_estimate(estimate: Estimate, actuator: Actuator) -> int:
    # One sizer, two terms, no phase branch:
    #     step = max( bulk = BULK_UNDERSHOOT * (window.lo - now),  fine = resolution * tick )
    # bulk carries `now` to the near edge of the boundary window in capped jumps over the dead
    # zone, then falls to <=0 once `now` is inside the window. The localize term is then a
    # FIXED fraction of a tick, so whichever step actually crosses brackets the boundary to
    # <= that fraction regardless of where in the window the boundary falls. Bisecting toward
    # window.hi (the old `(window.hi - now)/2`) instead left the bracket ~half the window
    # whenever the true boundary sat near the near edge — the wide-bracket bug.
    # The resolution is the one value carrying the regime: until a usable C interval exists
    # (zero or one crossing), the boundary prediction is unreliable, so window.lo == now,
    # bulk == 0, and COARSE searches for the next boundary fast; once two crossings give a real
    # interval, bulk jumps the dead zone and FINE brackets every subsequent boundary tightly.
    # The live tick estimate scales the resolution as C is learned. [LAW:dataflow-not-control-flow]
    # [LAW:no-mode-explosion]
    now = estimate.measured_cost
    tick = estimate.interval.mid if estimate.interval is not None else DEFAULT_TICK_COST[estimate.window]
    window = boundary_window(estimate, DEFAULT_TICK_COST[estimate.window])
    resolution = FINE_TICK_FRACTION if estimate.interval is not None else COARSE_TICK_FRACTION
    bulk_cost = BULK_UNDERSHOOT * (window.lo - now)
    fine_cost = resolution * tick
    step_cost = max(bulk_cost, fine_cost)
    target_blocks = step_cost / max(actuator.cost_per_block, 1e-12)
    target = int(actuator.header_tokens + target_blocks * actuator.tokens_per_block)
    # A single call cannot exceed the prompt cap, so the *honest* target is the achievable
    # one; reporting the pre-clamp number would misrepresent the step the loop actually takes.
    # A capped dead-zone jump simply takes several max calls; the fine term keeps every step
    # at least one resolution wide so the loop never stalls on a near-zero bulk. [FRAMING:representation]
    return max(MIN_PROMPT_TOKENS, min(MAX_PROMPT_TOKENS, target))


def fit_block_line(samples: list[tuple[int, int]]) -> tuple[float, float] | None:
    # Least-squares fit of input_tokens = a + b*blocks. Needs >=2 distinct block counts;
    # a single point cannot separate intercept from slope, so we hold the seed until then.
    xs = {blocks for blocks, _ in samples}
    if len(xs) < 2:
        return None
    n = len(samples)
    sx = sum(b for b, _ in samples)
    sy = sum(t for _, t in samples)
    sxx = sum(b * b for b, _ in samples)
    sxy = sum(b * t for b, t in samples)
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    if slope <= 0:
        return None
    return intercept, slope


def recalibrate(
    seed: Actuator,
    samples: list[tuple[int, int]],
    estimate: Estimate,
    total_blocks: int,
    model: str,
) -> tuple[Actuator, str | None]:
    # Pure: computes the actuator and, on cost divergence, the *text* of a warning — but
    # never emits it. drive() prints at the effect boundary. [LAW:effects-at-boundaries]
    fit = fit_block_line(samples)
    header_tokens, tokens_per_block = fit if fit is not None else (seed.header_tokens, seed.tokens_per_block)
    # Stage B: once the estimator has a cost interval, take cost/block from the
    # cost it actually measured (absorbs cache-write/output weighting by construction).
    # Otherwise hold the no-cache seed. Divergence from the prediction is reported, not hidden.
    warning: str | None = None
    if estimate.interval is not None and total_blocks > 0:
        cost_per_block = estimate.measured_cost / total_blocks
        predicted = tokens_per_block * model_input_cost_per_token(model)
        if predicted > 0 and cost_per_block > predicted * CACHE_WRITE_WARN_FACTOR:
            warning = (
                f"observed cost/block {cost_per_block:.6g} exceeds the no-cache prediction "
                f"{predicted:.6g} by >{CACHE_WRITE_WARN_FACTOR}x; cache-write tokens are "
                "inflating cost"
            )
    else:
        cost_per_block = tokens_per_block * model_input_cost_per_token(model)
    return Actuator(header_tokens, tokens_per_block, cost_per_block), warning


def served_input_tokens_since(path: Path, before_len: int) -> int | None:
    # Sum input_tokens over served (status-200) usage events appended after before_len.
    # None when no served event landed, so the caller never records a phantom sample.
    # before_len counts non-empty lines (see usage_log_len), so a corrupt line is kept as
    # a placeholder to preserve that index alignment — skipping it would misalign the slice
    # and silently drop a real later event. Its decode failure is surfaced, not absorbed.
    # [LAW:no-silent-failure]
    if not path.exists():
        return None
    rows: list[dict[str, Any]] = []
    with path.open() as f:
        for line_num, line in enumerate(f, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rows.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                print(f"warning: unparseable usage log line {line_num}: {exc}", file=sys.stderr)
                rows.append({})
    served_total = 0
    found = False
    for row in rows[before_len:]:
        if not isinstance(row, dict) or row.get("status") != 200:
            continue
        usage = row.get("usage")
        if not isinstance(usage, dict):
            continue
        served_total += int(number(usage.get("input_tokens")))
        found = True
    return served_total if found else None


def claude_env(run_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    proxy_url = env.get("PROXY_URL")
    if not proxy_url:
        die("PROXY_URL is required for active drive; use with_proxy.sh or pass --dry-run")
    env.update({
        "https_proxy": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "http_proxy": proxy_url,
        "HTTP_PROXY": proxy_url,
    })
    # Each `claude -p` otherwise fires a SECOND model call — a claude-haiku session-title
    # generation that re-sends the whole prompt — billed against the same quota and counted
    # by the estimator, so measured cost no longer represents the probe's own opus traffic.
    # This flag suppresses it: verified on real traffic (HAR) that a 60k-token prompt then
    # yields exactly one served opus event, no haiku. The exact token is read from the CLI
    # binary, not guessed. [LAW:effects-at-boundaries] fix the contaminating effect at its
    # source, not by subtracting it downstream; [FRAMING:representation] the measurement must
    # equal the thing it measures.
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    #
    # Two CLI behaviors each break the cost model; both must be off, and they masked each other:
    #  - the haiku title call (above) re-sent the prompt as input_tokens;
    #  - prompt caching shunts the prompt into cache_creation, leaving input_tokens ~6 and
    #    making per-call cost non-linear (create at >=1.25x, later read at 0.1x) -> disjoint
    #    crossing constraints. Disabling it bills the prompt as plain input_tokens, restoring
    #    the linear input_tokens = header + blocks*tokens_per_block the actuator assumes.
    # Verified on real traffic: with both off, a 60k-token prompt yields one opus event with
    # the whole prompt in input_tokens and cc=cr=0. [LAW:no-silent-failure]
    env["DISABLE_PROMPT_CACHING"] = "1"
    ca_cert = env.get("CCNB_CA_CERT")
    if ca_cert:
        env.update({
            "NODE_EXTRA_CA_CERTS": ca_cert,
            "SSL_CERT_FILE": ca_cert,
            "CURL_CA_BUNDLE": ca_cert,
            "REQUESTS_CA_BUNDLE": ca_cert,
        })
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    else:
        probe_config = run_dir.parent.parent / "probe-config"
        if probe_config.exists():
            env["CLAUDE_CONFIG_DIR"] = str(probe_config)
    return env


def run_claude(prompt: str, *, cfg: DriveConfig, iter_num: int, attempt: int) -> bool:
    # [LAW:no-silent-failure] Every attempt is recorded and a failure is reported
    # on stderr; the return value carries the outcome so the caller decides
    # whether to retry. A transient failure must never abort the whole run.
    run_dir = cfg.run_dir
    prompt_path = run_dir / "prompts" / f"{iter_num:03d}.txt"
    output_path = run_dir / "outputs" / f"{iter_num:03d}.txt"
    prompt_path.write_text(prompt)
    # These flags are load-bearing for the cost model: empty system prompt + no session
    # persistence + no tools keep each call a fresh, cache-free request whose cost is
    # dominated by the input tokens the actuator controls. Changing them can reintroduce
    # cache-write billing that the Stage-B observed-cost calibration would then have to absorb.
    cmd = [
        "claude",
        "-p",
        "--model",
        cfg.model,
        "--system-prompt",
        "",
        "--no-session-persistence",
        "--tools",
        "",
        "--",
        prompt,
    ]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            cmd,
            env=claude_env(run_dir),
            text=True,
            capture_output=True,
            timeout=cfg.claude_timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        output_path.write_text((exc.stdout or "") + (exc.stderr or ""))
        append_jsonl(run_dir / "driver-iterations.jsonl", {
            "iter": iter_num,
            "attempt": attempt,
            "ts": utc_now(),
            "prompt_chars": len(prompt),
            "duration_ms": elapsed_ms,
            "timed_out": True,
            "ok": False,
            "output_path": str(output_path),
        })
        print(
            f"warning: claude iter {iter_num} attempt {attempt} timed out after "
            f"{cfg.claude_timeout_seconds}s; see {output_path}",
            file=sys.stderr,
        )
        return False
    elapsed_ms = int((time.monotonic() - started) * 1000)
    output_path.write_text(completed.stdout + completed.stderr)
    ok = completed.returncode == 0
    append_jsonl(run_dir / "driver-iterations.jsonl", {
        "iter": iter_num,
        "attempt": attempt,
        "ts": utc_now(),
        "prompt_chars": len(prompt),
        "duration_ms": elapsed_ms,
        "exit_code": completed.returncode,
        "ok": ok,
        "output_path": str(output_path),
    })
    if not ok:
        print(
            f"warning: claude iter {iter_num} attempt {attempt} exited "
            f"{completed.returncode}; see {output_path}",
            file=sys.stderr,
        )
    return ok


def attempt_claude_call(prompt: str, *, cfg: DriveConfig, iter_num: int, usage_path: Path) -> bool:
    # Bounded retry with backoff. Returns True once a call succeeds AND its served
    # usage event lands in the log. A failed attempt's traffic is still logged by
    # the proxy as non-200 and is excluded from the total downstream, so retrying
    # never double-counts. [LAW:no-ambient-temporal-coupling] the backoff schedule
    # is owned here, not assumed elsewhere.
    for attempt in range(MAX_CALL_ATTEMPTS):
        before = usage_log_len(usage_path)
        if run_claude(prompt, cfg=cfg, iter_num=iter_num, attempt=attempt):
            if wait_for_usage_event(usage_path, before):
                return True
            print(
                f"warning: claude iter {iter_num} attempt {attempt} succeeded but "
                "no usage event landed; retrying",
                file=sys.stderr,
            )
        if attempt + 1 < MAX_CALL_ATTEMPTS:
            time.sleep(RETRY_BACKOFF_SECONDS * (2 ** attempt))
    return False


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def usage_log_len(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open() as f:
        return sum(1 for line in f if line.strip())


def wait_for_usage_event(path: Path, previous_len: int) -> bool:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if usage_log_len(path) > previous_len:
            return True
        time.sleep(0.1)
    return False


def synthetic_input_tokens(blocks: int) -> int:
    return max(1, int(round(DRYRUN_TRUE_HEADER_TOKENS + blocks * DRYRUN_TRUE_TOKENS_PER_BLOCK)))


def synthetic_event(*, input_tokens: int, util_bucket: int, model: str, request_id: str) -> dict[str, Any]:
    # Fresh-call model: cost lives in input tokens (no cache), so the estimator prices it
    # through the 1.0x input weight and the token actuator is genuinely exercised.
    return {
        "ts": utc_now(),
        "upstream": "api.anthropic.com",
        "model": model,
        "status": 200,
        "duration_ms": 1,
        "streaming": False,
        "errors": [],
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_creation_1h_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        "quota": {
            "five_hour_utilization": util_bucket / 100.0,
            "seven_day_utilization": util_bucket / 100.0,
        },
        "meta": {
            "organization_id": "dry-run-org",
            "request_id": request_id,
        },
    }


def append_synthetic_event(run_dir: Path, *, cfg: DriveConfig, actual_spend: float, blocks: int, iter_num: int) -> float:
    input_tokens = synthetic_input_tokens(blocks)
    # seed_actuator already rejected unknown models, so None here is a broken invariant,
    # not an expected input — surface it instead of coercing to a zero-cost event that
    # would silently corrupt the estimate. [LAW:no-silent-failure]
    cost = request_cost(cfg.model, {"input_tokens": input_tokens})
    if cost is None:
        die(f"dry-run pricing returned no cost for model {cfg.model!r}; seed_actuator should have rejected it")
    tick = DEFAULT_TICK_COST[cfg.window]
    new_spend = actual_spend + cost
    util_bucket = min(100, int(new_spend / tick))
    append_jsonl(
        run_dir / "usage.jsonl",
        synthetic_event(
            input_tokens=input_tokens,
            util_bucket=util_bucket,
            model=cfg.model,
            request_id=f"dry_{iter_num:03d}",
        ),
    )
    return new_spend


def write_manifest(run_dir: Path, cfg: DriveConfig) -> None:
    manifest = {
        "schema_version": 1,
        "started": utc_now(),
        "driver": "tools/quota_probe/measure.py",
        "config": cfg.to_json(),
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def write_result(run_dir: Path, estimate: Estimate) -> None:
    data = estimate.to_json()
    (run_dir / "fresh-bounds.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    (run_dir / "fresh-report.md").write_text(render_report(data))


def render_report(data: dict[str, Any]) -> str:
    lines = [
        "# Fresh Quota Probe Report",
        "",
        f"- Status: `{data['status']}`",
        f"- Reason: `{data['reason']}`" if data["reason"] else "- Reason: `ok`",
        f"- Window: `{data['window']}`",
        f"- Events: loaded={data['events']['loaded']} priced={data['events']['priced']} excluded={data['events']['excluded']}",
        f"- Crossings: {data['crossing_count']}",
        "",
    ]
    if data["cost_per_tick"] is None:
        lines.append("No capacity estimate is available from this artifact set.")
    else:
        unit = data["cost_unit"]
        per_tick = data["cost_per_tick"]
        full = data["cost_full_quota"]
        lines.extend([
            "## Estimate",
            "",
            f"- Cost / 1% tick ({unit}): low={per_tick['low']:.0f} mid={per_tick['midpoint']:.0f} high={per_tick['high']:.0f}",
            f"- Relative interval width: {per_tick['relative_width'] * 100:.3f}%",
            f"- Full quota ({unit}): low={full['low']:.0f} mid={full['midpoint']:.0f} high={full['high']:.0f}",
        ])
    if data["exclusions"]:
        lines.extend(["", "## Exclusions", ""])
        for ex in data["exclusions"][:20]:
            lines.append(f"- line {ex['line']}: `{ex['reason']}` {ex['detail']}".rstrip())
    lines.append("")
    return "\n".join(lines)


def estimate_run(run_dir: Path, window: str) -> Estimate:
    usage_path = run_dir / "usage.jsonl"
    if not usage_path.exists():
        die(f"missing usage log: {usage_path}")
    estimate = estimate_usage_log(usage_path, window=window)
    write_result(run_dir, estimate)
    return estimate


def reject_unusable_active_events(estimate: Estimate) -> None:
    if estimate.loaded_events > 0 and estimate.priced_events == 0:
        reasons = sorted({ex.reason for ex in estimate.exclusions})
        reason_text = ", ".join(reasons) if reasons else "no priced events"
        die(
            "active run produced API events but none had usable usage/quota data "
            f"({reason_text}); see fresh-report.md and usage.jsonl"
        )


def stop_reached(estimate: Estimate, cfg: DriveConfig) -> bool:
    # One stop predicate; which threshold governs is selected by whether a
    # tick target carries a value, not by a separate run mode.
    # [LAW:dataflow-not-control-flow] [LAW:no-mode-explosion]
    if cfg.target_ticks is not None:
        return len(estimate.crossings) >= cfg.target_ticks
    return estimate.interval is not None and estimate.interval.relative_width <= cfg.target_relative_width


def drive(cfg: DriveConfig) -> Estimate:
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    (cfg.run_dir / "prompts").mkdir(exist_ok=True)
    (cfg.run_dir / "outputs").mkdir(exist_ok=True)
    write_manifest(cfg.run_dir, cfg)

    usage_path = cfg.run_dir / "usage.jsonl"
    seed = seed_actuator(cfg.model)
    actuator = seed
    # Served-iter samples (blocks emitted, observed input_tokens) drive Stage A; total_blocks
    # over served iters drives Stage B. Both come from served events only — never from prompt
    # files on disk, which would include skipped/failed iters. [LAW:one-source-of-truth]
    samples: list[tuple[int, int]] = []
    total_blocks = 0
    dry_spend = DEFAULT_TICK_COST[cfg.window] * 23.37

    # Warmup at blocks=0: a served event at the Stage-A intercept, anchoring header_tokens.
    before = usage_log_len(usage_path)
    if cfg.dry_run:
        dry_spend = append_synthetic_event(cfg.run_dir, cfg=cfg, actual_spend=dry_spend, blocks=0, iter_num=0)
    else:
        if not attempt_claude_call(build_prompt(0), cfg=cfg, iter_num=0, usage_path=usage_path):
            die("initial claude call failed after retries; cannot start measurement")
    observed = served_input_tokens_since(usage_path, before)
    if observed is not None:
        samples.append((0, observed))

    estimate = estimate_run(cfg.run_dir, cfg.window)
    if not cfg.dry_run:
        reject_unusable_active_events(estimate)
    actuator, warning = recalibrate(seed, samples, estimate, total_blocks, cfg.model)
    if warning:
        print(f"warning: {warning}", file=sys.stderr)

    consecutive_failures = 0
    for iter_num in range(1, cfg.max_iters + 1):
        target_tokens = target_input_tokens_for_estimate(estimate, actuator)
        blocks = blocks_for_target_tokens(target_tokens, actuator)
        prompt = build_prompt(blocks)
        before = usage_log_len(usage_path)
        if cfg.dry_run:
            (cfg.run_dir / "prompts" / f"{iter_num:03d}.txt").write_text(prompt)
            (cfg.run_dir / "outputs" / f"{iter_num:03d}.txt").write_text("ok\n")
            dry_spend = append_synthetic_event(cfg.run_dir, cfg=cfg, actual_spend=dry_spend, blocks=blocks, iter_num=iter_num)
        else:
            if not attempt_claude_call(prompt, cfg=cfg, iter_num=iter_num, usage_path=usage_path):
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    die(
                        f"claude failed {consecutive_failures} iterations in a row after "
                        "retries; cannot make progress"
                    )
                print(
                    f"warning: iter {iter_num} failed after retries; skipping "
                    f"(consecutive failures: {consecutive_failures})",
                    file=sys.stderr,
                )
                continue
            consecutive_failures = 0
        observed = served_input_tokens_since(usage_path, before)
        if observed is not None:
            samples.append((blocks, observed))
            total_blocks += blocks
        estimate = estimate_run(cfg.run_dir, cfg.window)
        if not cfg.dry_run:
            reject_unusable_active_events(estimate)
        actuator, warning = recalibrate(seed, samples, estimate, total_blocks, cfg.model)
        if warning:
            print(f"warning: {warning}", file=sys.stderr)
        append_jsonl(cfg.run_dir / "driver-iterations.jsonl", {
            "iter": iter_num,
            "ts": utc_now(),
            "input_tokens_target": target_tokens,
            "blocks": blocks,
            "observed_input_tokens": observed,
            "tokens_per_block": actuator.tokens_per_block,
            "cost_per_block": actuator.cost_per_block,
            "relative_width": None if estimate.interval is None else estimate.interval.relative_width,
        })
        if stop_reached(estimate, cfg):
            return estimate
    return estimate


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fresh Claude Code quota measurement")
    sub = p.add_subparsers(dest="cmd", required=True)

    report = sub.add_parser("report", help="estimate from an existing run directory")
    report.add_argument("run_dir", type=Path)
    report.add_argument("--window", choices=("5h", "7d"), required=True)
    report.add_argument("--print", dest="print_result", action="store_true")

    drive_cmd = sub.add_parser("drive", help="generate traffic, then estimate from usage.jsonl")
    drive_cmd.add_argument("--run-dir", type=Path)
    drive_cmd.add_argument("--window", choices=("5h", "7d"), required=True)
    drive_cmd.add_argument("--model", default=DEFAULT_MODEL)
    drive_cmd.add_argument("--target-relative-width", type=float, default=0.03)
    drive_cmd.add_argument(
        "--target-ticks",
        type=int,
        default=None,
        help="stop after observing this many quota-tick crossings instead of a target width; "
        "defaults to the TARGET_5H_TICKS / TARGET_7D_TICKS env var for the chosen window",
    )
    drive_cmd.add_argument("--max-iters", type=int, default=80)
    drive_cmd.add_argument("--claude-timeout-seconds", type=int, default=DEFAULT_CLAUDE_TIMEOUT_SECONDS)
    drive_cmd.add_argument("--dry-run", action="store_true")

    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.cmd == "report":
        estimate = estimate_run(args.run_dir, args.window)
        if args.print_result:
            print(render_report(estimate.to_json()))
        return
    if args.cmd == "drive":
        run_dir = args.run_dir or default_run_dir(args.dry_run)
        # The window→env-var mapping lives only here, where the window is known,
        # so callers (justfile, with_proxy.sh) never re-derive it. [LAW:single-enforcer]
        target_ticks = args.target_ticks
        if target_ticks is None:
            env_ticks = os.environ.get(f"TARGET_{args.window.upper()}_TICKS")
            if env_ticks is not None:
                target_ticks = int(env_ticks)
        cfg = DriveConfig(
            window=args.window,
            run_dir=run_dir,
            model=args.model,
            target_relative_width=args.target_relative_width,
            max_iters=args.max_iters,
            dry_run=args.dry_run,
            target_ticks=target_ticks,
            claude_timeout_seconds=args.claude_timeout_seconds,
        )
        estimate = drive(cfg)
        print(render_report(estimate.to_json()))
        if estimate.status != "estimated":
            raise SystemExit(2)


if __name__ == "__main__":
    main()
