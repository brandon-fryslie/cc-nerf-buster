#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen

from rich.console import Console, Group
from rich.live import Live
from rich.rule import Rule
from rich.spinner import Spinner
from rich.text import Text


PROMPT_HEADER = """Read the operational notes below and reply with one short sentence:
The notes describe normal service activity.
Do not add anything else.
"""

# Sizes calibrated against measured cost on Opus 4.7 (~0.75 input-equiv per
# prompt char, dominated by cache_write × 2). At ~550k input-equiv per 1%
# tick: large ≈ 12.5% of tick. medium/small/micro need re-measurement after
# the per-call overhead was eliminated. Micro is dynamic.
PROMPT_CHAR_TARGETS = {
    "large": 550_000,
    "medium": 25_000,
    "small": 3_000,
    "micro": 200,
}

PROMPT_PARAGRAPHS = (
    "Operations staff reviewed a routine sample of service activity and found that the request mix stayed within the same general range seen on other uneventful days. Most traffic came from ordinary interactive work, while a smaller share came from scheduled batch jobs, background indexing, and occasional maintenance tasks. Nothing in the notes suggested a sudden shift in customer behavior, a widespread outage, or a meaningful change in the way teams were using the service. The main conclusion was simply that the environment looked familiar, understandable, and steady enough to support planning.",
    "Several parts of the notes focused on instrumentation quality rather than incident response. Engineers verified that quota headers were still present when expected, that structured logs were continuing to land in their canonical location, and that request tracing still exposed enough information to reconstruct usage after the fact. They emphasized that measurement systems should remain easy to audit. In their view, a reliable probe is not one that is clever or novel, but one that generates ordinary traffic, yields a constrained answer, and leaves behind a record that another person can inspect without guesswork.",
    "Another section summarized the shape of customer work in plain language. Some teams were reviewing documents, some were asking short coding questions, and others were running repetitive operational checks. Analysts noted that the workload was broad without being chaotic. Short prompts, long prompts, and scheduled jobs were all present, but none of them dominated to an unusual extent. This mattered because the team wanted the probe workload to resemble normal service usage closely enough that the resulting quota estimates would remain relevant instead of being distorted by exotic prompting patterns.",
    "The notes also described recurring housekeeping tasks performed by operators. They compared daily reports, checked that proxies were forwarding expected headers, and confirmed that stream handling still produced usable records in both success and failure cases. A few comments mentioned that prompt caching could affect how much work was actually billed, which is why the probe should prefer large plain-English inputs with small predictable outputs. The purpose was not to trick the model, but to produce repeated, comprehensible requests whose token footprint could be reasoned about later.",
    "One memo discussed process choices that had already proven unhelpful. The team explicitly rejected complicated calibration schemes, one-off prompt gimmicks, and elaborate control systems that were hard to explain after the run ended. They wanted a straightforward routine: send a normal prompt, observe the resulting usage, watch for utilization ticks, and adjust prompt size only when needed to avoid wasting the remaining distance to the next boundary. That approach was favored because it reduced hidden assumptions and kept the measurement path aligned with the actual goal of the repository.",
    "A final review paragraph described how engineers wanted to talk about the system internally. They preferred to say that the service experienced normal operating conditions, predictable request handling, stable logging, and no material surprises. They also wanted the probe prompts to read like ordinary prose instead of synthetic filler. If a future reader opened a saved prompt file, that reader should see a plain operational note, not a string of tokens that only existed to exploit a tokenizer. The documentation stressed that plain language improves auditability even when the real objective is numerical measurement.",
    "The operational summary returned several times to the same theme: ordinary traffic is more useful than fancy traffic. The proxy should observe requests that look like normal written material, the response should be constrained enough that output variance remains small, and the measurement logic should avoid inventing new sources of truth. When these conditions are met, a probe run can be resumed, explained, and compared with earlier runs without a lot of interpretive work. That was considered more valuable than squeezing a tiny theoretical gain from a prompt that no one would willingly read or maintain.",
    "The team also wrote about expected limits on variability. They accepted that output tokens cannot be made perfectly constant, but they still wanted a response target that is short, boring, and stable. They accepted that utilization ticks are only visible at whole-percent boundaries, but they still wanted the prompt ladder to be simple enough that operators could understand why the script selected a given size. In general, the preference was for transparent tradeoffs over opaque optimization. A boring probe that behaves consistently is easier to trust than a clever probe whose behavior is hard to predict.",
)


def build_prompt_corpus(target_chars: int) -> str:
    parts = [PROMPT_HEADER.strip(), "", "Operational notes:"]
    idx = 0
    while len("\n\n".join(parts)) < target_chars:
        section_num = idx + 1
        paragraph = PROMPT_PARAGRAPHS[idx % len(PROMPT_PARAGRAPHS)]
        parts.append(f"Section {section_num}")
        parts.append(paragraph)
        idx += 1
    return "\n\n".join(parts) + "\n"


# Output is ~15 tokens at 5x weighting = a fixed ~75 input-equiv floor every
# call regardless of prompt size. Anthropic begins cache-writing (×2) at ~1024
# input tokens, so we keep micro strictly below that to stay on plain-input
# pricing — each input token costs 1 input-equiv, ~3.6 chars per token.
MICRO_OUTPUT_FLOOR = 75.0
MICRO_CACHE_THRESHOLD_TOKENS = 900
MICRO_CHARS_PER_TOKEN = 3.6
MICRO_BASE = "Reply with: ok.\n"
MICRO_FILLER = "Note: routine operational sample data point. "


def build_micro_prompt(target_input_equiv: float) -> str:
    target_input_tokens = max(3.0, target_input_equiv - MICRO_OUTPUT_FLOOR)
    target_input_tokens = min(target_input_tokens, MICRO_CACHE_THRESHOLD_TOKENS)
    target_chars = int(target_input_tokens * MICRO_CHARS_PER_TOKEN)
    if target_chars <= len(MICRO_BASE):
        return MICRO_BASE
    repeats = (target_chars - len(MICRO_BASE)) // len(MICRO_FILLER) + 1
    body = (MICRO_FILLER * repeats)[: target_chars - len(MICRO_BASE)]
    return MICRO_BASE + body + "\n"


# Lead-bracket sizing. The leading bracket has different objectives than the
# measurement: we don't measure cost during it, but we want the crossing-call
# to be small so overshoot is bounded — measurement #1 then starts with a
# tight, known position. Fixed ~5% of est tick achieves that, at the cost of
# more iters in the leading block.
#
# Empirical char-to-input-equiv ratio (from `large` measurements with cache
# write at 2×): ~0.625 input-equiv/char. Needs re-verification after the
# first clean run with the probe-config CLAUDE_CONFIG_DIR approach.
LEAD_TICK_FRACTION = 0.05
LEAD_PROMPT_INPUT_EQUIV_PER_CHAR = 0.625


def build_lead_prompt(est_units_per_tick: float) -> str:
    target_chars = int(LEAD_TICK_FRACTION * est_units_per_tick / LEAD_PROMPT_INPUT_EQUIV_PER_CHAR)
    return build_prompt_corpus(target_chars)


PROMPTS = {
    name: build_prompt_corpus(target_chars)
    for name, target_chars in PROMPT_CHAR_TARGETS.items()
    if name != "micro"
}

PROMPT_STATS = {
    name: {"chars": len(text), "words": len(text.split())}
    for name, text in PROMPTS.items()
}
PROMPT_STATS["micro"] = {"chars": -1, "words": -1}  # dynamic, sized per call
PROMPT_STATS["lead"] = {"chars": -1, "words": -1}   # dynamic, sized per call

# // [LAW:one-source-of-truth] per-window defaults live in one map keyed by
# the window string. Every site that needs a per-window default reads from
# here — there is no separate constant per window to drift out of sync.
WINDOW_CHOICES = ("5h", "7d")
DEFAULT_INPUT_EQUIV_PER_TICK = {
    "5h": 550_623.0,
    "7d": 550_623.0 * 5,
}
DEFAULT_TARGET_TICKS = {
    "5h": 3,
    "7d": 1,
}

OUTPUT_TO_INPUT_EQUIV = 5.0
CACHE_CREATE_TO_INPUT_EQUIV = 2.0
CACHE_READ_TO_INPUT_EQUIV = 0.10

# // [LAW:one-source-of-truth] OCW (Opus Cache Write equivalent) is the
# canonical display unit. The internal weighting expresses costs in
# input-equivalent units (input=1×, output=5×, cache_create=2×); since
# cache_create is 2× input, OCW = input_equivalent / 2.
def to_ocw(input_equiv: float) -> float:
    return input_equiv / CACHE_CREATE_TO_INPUT_EQUIV


FATAL_CLAUDE_OUTPUT_PATTERNS = (
    "Not logged in",
    "Please run /login",
    "Input must be provided either through stdin or as a prompt argument",
)


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_ts() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


_USE_COLOR = sys.stderr.isatty()


def _c(s: object, code: str) -> str:
    if not _USE_COLOR:
        return str(s)
    return f"\033[{code}m{s}\033[0m"


def dim(s: object) -> str: return _c(s, "2")


def log(msg: str) -> None:
    ts = datetime.now(UTC).strftime("%H:%M:%S")
    print(f"{dim(f'[{ts}]')} {msg}", file=sys.stderr)


def die(msg: str) -> "NoReturn":
    log(f"ERROR: {msg}")
    raise SystemExit(1)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--dry-run", action="store_true")
    # // [LAW:one-type-per-behavior] one window per run. The "both" mode used
    # to exist but was fundamentally broken: a single prompt-size knob cannot
    # binary-search two tick boundaries with different per-tick costs at once.
    p.add_argument("--window", choices=WINDOW_CHOICES, required=True)
    p.add_argument("--resume", default=None)
    p.add_argument("--continue", dest="continue_latest", action="store_true")
    args = p.parse_args()
    if args.resume and args.continue_latest:
        die("use either --resume <run_dir> or --continue, not both")
    return args


def fetch_metrics(metrics_url: str) -> str:
    with urlopen(metrics_url) as resp:
        body = resp.read().decode("utf-8")
    if not body.strip():
        die(f"empty /metrics body from {metrics_url}")
    return body


def parse_gauge(metrics_text: str, name: str) -> float:
    prefix_a = f"{name}{{"
    prefix_b = f"{name} "
    for line in metrics_text.splitlines():
        if line.startswith(prefix_a) or line.startswith(prefix_b):
            return float(line.rsplit(" ", 1)[-1])
    return 0.0


def parse_metric_line(line: str) -> tuple[str, dict[str, str], float] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    head, sep, value_text = line.rpartition(" ")
    if not sep:
        return None
    try:
        value = float(value_text)
    except ValueError:
        return None
    if "{" not in head:
        return head, {}, value
    name, _, labels_text = head.partition("{")
    labels_text = labels_text.removesuffix("}")
    labels: dict[str, str] = {}
    for item in labels_text.split(","):
        key, _, raw_value = item.partition("=")
        labels[key] = raw_value.strip('"')
    return name, labels, value


def matching_series(metrics_text: str, metric_name: str, required_labels: dict[str, str]) -> list[tuple[dict[str, str], float]]:
    matches: list[tuple[dict[str, str], float]] = []
    for line in metrics_text.splitlines():
        parsed = parse_metric_line(line)
        if parsed is None:
            continue
        name, labels, value = parsed
        if name != metric_name:
            continue
        if all(labels.get(key) == expected for key, expected in required_labels.items()):
            matches.append((labels, value))
    return matches


def canonical_metric_scope(metrics_text: str, model: str) -> dict[str, str]:
    # [LAW:one-source-of-truth] the active model request series defines the
    # org/upstream identity for the rest of the probe's metrics snapshot.
    # A fresh proxy has no series yet — return an empty scope so the snapshot
    # reads zeros uniformly instead of branching on "is the proxy fresh?".
    series = matching_series(metrics_text, "ccnb_requests_total", {"model": model})
    if not series:
        return {}
    labels, _value = max(series, key=lambda item: item[1])
    scope = {key: labels[key] for key in ("org", "upstream") if key in labels}
    if len(scope) != 2:
        die(f"unable to resolve canonical org/upstream scope for model {model}")
    return scope


def parse_scoped_counter_total(
    metrics_text: str,
    metric_name: str,
    required_labels: dict[str, str],
) -> int:
    total = 0
    for _labels, value in matching_series(metrics_text, metric_name, required_labels):
        total += int(value)
    return total


def parse_scoped_gauge(metrics_text: str, metric_name: str, required_labels: dict[str, str]) -> float:
    # A fresh proxy has not emitted any scoped series yet; treat absence as 0.0
    # so the snapshot's value flow is the same on a fresh proxy as on a populated one.
    series = matching_series(metrics_text, metric_name, required_labels)
    if not series:
        return 0.0
    return series[0][1]


def snapshot_metrics(run_dir: Path, metrics_url: str, label: str, model: str) -> dict:
    raw_text = fetch_metrics(metrics_url)
    raw_path = run_dir / "raw-metrics" / f"{label}.prom"
    raw_path.write_text(raw_text)
    scope = canonical_metric_scope(raw_text, model)
    model_scope = {"model": model, **scope}

    # Both util_5h and util_7d are emitted by /metrics regardless of which
    # window the probe is driving — capturing both keeps the report.py view
    # informative without affecting which window controls the loop.
    snap = {
        "label": label,
        "ts": utc_now(),
        "org": scope.get("org", ""),
        "upstream": scope.get("upstream", ""),
        "util_5h": parse_scoped_gauge(raw_text, "ccnb_quota_5h_utilization", scope),
        "util_7d": parse_scoped_gauge(raw_text, "ccnb_quota_7d_utilization", scope),
        "cost_total": parse_scoped_gauge(raw_text, "ccnb_cost_total", scope),
        "capacity_usd_5h": parse_scoped_gauge(raw_text, "ccnb_quota_5h_estimated_capacity_usd", scope),
        "capacity_usd_7d": parse_scoped_gauge(raw_text, "ccnb_quota_7d_estimated_capacity_usd", scope),
        "no_model_input_tokens": parse_gauge(raw_text, "ccnb_no_model_error_input_tokens_total"),
        "no_model_output_tokens": parse_gauge(raw_text, "ccnb_no_model_error_output_tokens_total"),
        "model_requests": parse_scoped_counter_total(raw_text, "ccnb_requests_total", model_scope),
        "model_input_tokens": parse_scoped_counter_total(raw_text, "ccnb_input_tokens_total", model_scope),
        "model_output_tokens": parse_scoped_counter_total(raw_text, "ccnb_output_tokens_total", model_scope),
        "model_cache_creation_input_tokens": parse_scoped_counter_total(raw_text, "ccnb_cache_creation_input_tokens_total", model_scope),
        "model_cache_read_input_tokens": parse_scoped_counter_total(raw_text, "ccnb_cache_read_input_tokens_total", model_scope),
    }
    with (run_dir / "snapshots.jsonl").open("a") as f:
        f.write(json.dumps(snap) + "\n")
    return snap


def tick_delta(current_util: float, baseline_util: float) -> int:
    return int(current_util * 100 + 1e-9) - int(baseline_util * 100 + 1e-9)


def is_zero_util(util: float) -> bool:
    return abs(util) <= 1e-9


def input_price_per_mtok(model: str) -> float:
    model_lc = model.lower()
    if "haiku" in model_lc:
        return 1.0
    if "sonnet" in model_lc:
        return 3.0
    return 5.0


def choose_prompt_size(
    ticks_seen: int,
    need: int,
    est_units_per_tick: float,
    used_units_since_tick: float,
    is_leading_bracket: bool = False,
) -> tuple[str, float]:
    # // [LAW:dataflow-not-control-flow] every iteration follows the same
    # choose-build-send-measure sequence; only the prompt size value changes.
    #
    # Two regimes, selected by `is_leading_bracket`:
    #
    #   * Leading bracket: fixed ~5% of tick. The leading block is not a
    #     measurement; its job is to anchor at the next 1% boundary with
    #     small overshoot so that measurement #1 starts cleanly. We accept
    #     more iters here as the cost of bounded bracket error.
    #
    #   * Measurement: ladder large→medium→small→micro, sized so each tier
    #     is used until ~one-call-worth of its own size remains. Per-call
    #     costs need re-measurement after the first clean run; thresholds
    #     were calibrated before per-call overhead was eliminated.
    if ticks_seen >= need or est_units_per_tick <= 0:
        return ("large", 0.0)
    remaining_units = max(0.0, est_units_per_tick - used_units_since_tick)
    if is_leading_bracket:
        return ("lead", remaining_units)
    remaining_ratio = remaining_units / est_units_per_tick
    if remaining_ratio > 0.15:
        size = "large"
    elif remaining_ratio > 0.04:
        size = "medium"
    elif remaining_ratio > 0.015:
        size = "small"
    else:
        size = "micro"
    return size, remaining_units


def build_prompt(
    size_name: str,
    *,
    micro_target_input_equiv: float = 0.0,
    lead_est_units_per_tick: float = 0.0,
) -> str:
    if size_name == "micro":
        return build_micro_prompt(micro_target_input_equiv)
    if size_name == "lead":
        return build_lead_prompt(lead_est_units_per_tick)
    return PROMPTS[size_name]


def quota_input_equivalent_tokens(usage: dict) -> float:
    return (
        float(usage["input"])
        + OUTPUT_TO_INPUT_EQUIV * float(usage["output"])
        + CACHE_CREATE_TO_INPUT_EQUIV * float(usage["cache_create"])
        + CACHE_READ_TO_INPUT_EQUIV * float(usage["cache_read"])
    )


def summarize_output(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:240]
    return "(no output)"


def fatal_output_reason(text: str) -> str | None:
    for pattern in FATAL_CLAUDE_OUTPUT_PATTERNS:
        if pattern in text:
            return pattern
    return None


def resume_command_for_run(window: str, run_dir: Path | None) -> str:
    recipe = {"5h": "probe-5h", "7d": "probe-7d"}[window]
    if run_dir is None:
        return f"just {recipe}"
    return f"just {recipe} --resume {run_dir}"


def run_report(script_dir: Path, run_dir: Path) -> None:
    # // [LAW:single-enforcer] the in-terminal "Result" panel + comparison block
    # is the single human-facing summary surface. report.py runs silently here —
    # it still writes report.md / bounds.json / crossings.jsonl for audit, and
    # users who want the bounds view post-hoc invoke `just probe-bounds`.
    subprocess.run(
        [sys.executable, str(script_dir / "report.py"), str(run_dir)],
        check=True,
    )


def find_previous_run(data_dir: Path, current_run_dir: Path, window: str) -> Path | None:
    runs_dir = data_dir / "probe-runs"
    if not runs_dir.exists():
        return None
    candidates: list[Path] = []
    for child in runs_dir.iterdir():
        if not child.is_dir() or child == current_run_dir:
            continue
        if child.name.startswith("dryrun-"):
            continue
        if not (child / "bounds.json").exists():
            continue
        manifest_path = child / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError:
            continue
        # Only compare to runs that were driven against the same window —
        # otherwise the comparison is across measurement targets and
        # misleading.
        if manifest.get("window") != window:
            continue
        candidates.append(child)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.name)


@dataclass
class _PreviousRunComparison:
    """Resolved measurement of the most recent comparable run, in OCW/tick.

    bounds.json stores per-model token projections (e.g. opus input tokens
    per tick). The Result panel speaks OCW (Opus Cache Write equivalent)
    units. Convert at the boundary so the panel can render the comparison
    in the same unit as its headline — mixing units would be a unit-error
    waiting to happen.
    """
    run_name: str
    clean_ticks: int
    ocw_per_tick: float


def compute_previous_comparison(
    data_dir: Path,
    current_run_dir: Path,
    window: str,
) -> _PreviousRunComparison | None:
    prev_run = find_previous_run(data_dir, current_run_dir, window)
    if prev_run is None:
        return None
    bounds_path = prev_run / "bounds.json"
    if not bounds_path.exists():
        return None
    try:
        prev = json.loads(bounds_path.read_text())
    except json.JSONDecodeError:
        return None
    pw = prev.get("windows", {}).get(window) or {}
    clean_ticks = pw.get("clean_measured_ticks", 0)
    if clean_ticks == 0:
        return None
    opus = ((pw.get("tokens") or {}).get("midpoint") or {}).get("opus") or {}
    input_per_tick = opus.get("input_per_tick")
    if input_per_tick is None:
        return None
    return _PreviousRunComparison(
        run_name=prev_run.name,
        clean_ticks=clean_ticks,
        ocw_per_tick=input_per_tick / CACHE_CREATE_TO_INPUT_EQUIV,
    )


def copy_with_hashes(run_dir: Path, script_dir: Path) -> None:
    copied = [
        Path(__file__),
        script_dir / "probe.sh",
        script_dir / "report.py",
    ]
    hashes: list[str] = []
    for src in copied:
        dst = run_dir / src.name
        shutil.copy2(src, dst)
        hashes.append(f"{hashlib.sha256(src.read_bytes()).hexdigest()}  {src.name}")
    (run_dir / "scripts.sha256").write_text("\n".join(hashes) + "\n")


def claude_env(data_dir: Path) -> dict[str, str]:
    ca_cert = data_dir / "ca.crt"
    # [LAW:one-source-of-truth] PROXY_URL is set by the wrapper that owns the
    # cc-nerf-buster process; the probe just consumes it.
    proxy_url = os.environ.get("PROXY_URL", "http://localhost:9480")
    env = dict(os.environ)
    # [LAW:single-enforcer] the probe owns the Claude subprocess environment so
    # every probe request goes through the same proxy/CA boundary.
    # CLAUDE_CONFIG_DIR points at the probe-config dir which has credentials
    # but no CLAUDE.md — eliminating the ~13K OCW per-call overhead that the
    # global ~/.claude/CLAUDE.md would otherwise inject into every request.
    env.update(
        {
            "https_proxy": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "http_proxy": proxy_url,
            "HTTP_PROXY": proxy_url,
            "NODE_EXTRA_CA_CERTS": str(ca_cert),
            "SSL_CERT_FILE": str(ca_cert),
            "CURL_CA_BUNDLE": str(ca_cert),
            "REQUESTS_CA_BUNDLE": str(ca_cert),
            "GIT_SSL_CAINFO": str(ca_cert),
            "CLAUDE_CONFIG_DIR": str(data_dir / "probe-config"),
        }
    )
    return env


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open() as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def load_resume_state(run_dir: Path, window: str) -> tuple[dict, dict, int, float, int]:
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        die(f"resume run missing manifest: {manifest_path}")

    manifest = json.loads(manifest_path.read_text())
    if manifest.get("window") != window:
        die(f"resume mismatch: run was --window={manifest.get('window')!r}, you passed --window={window!r}")
    baseline = manifest["baseline"]
    est_units_per_tick = float(manifest["estimated_input_equiv_tokens_per_tick"])
    util_key = f"util_{window}"

    snapshots = {row["label"]: row for row in load_jsonl(run_dir / "snapshots.jsonl")}
    iterations = load_jsonl(run_dir / "iterations.jsonl")

    current = baseline
    used_units_since_tick = 0.0
    msgs_since_tick = 0

    for row in iterations:
        label = f"{int(row['iter']):03d}-after"
        snap = snapshots.get(label)
        if snap is None:
            break

        iter_units = float(row["input_equivalent_tokens"])
        prev_ticks = tick_delta(current[util_key], baseline[util_key])
        new_ticks = tick_delta(snap[util_key], baseline[util_key])
        crossed = max(0, new_ticks - prev_ticks)

        used_units_since_tick += iter_units
        msgs_since_tick += 1
        if crossed > 0 and est_units_per_tick > 0:
            used_units_since_tick = max(0.0, used_units_since_tick - est_units_per_tick * crossed)
            msgs_since_tick = 0

        current = snap

    next_iter = 1
    if iterations:
        next_iter = int(iterations[-1]["iter"]) + 1

    return current, baseline, next_iter, used_units_since_tick, msgs_since_tick


def resolve_continue_run(data_dir: Path, window: str, dry_run: bool) -> Path:
    probe_runs_dir = data_dir / "probe-runs"
    if not probe_runs_dir.exists():
        die(f"no probe runs found under {probe_runs_dir}")

    candidates: list[Path] = []
    for child in probe_runs_dir.iterdir():
        if not child.is_dir():
            continue
        manifest_path = child / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError:
            continue
        if manifest.get("window") != window:
            continue
        if bool(manifest.get("dry_run")) != dry_run:
            continue
        candidates.append(child)

    if not candidates:
        die(f"no {'dry-run ' if dry_run else ''}{window} probe runs found under {probe_runs_dir}")

    return max(candidates, key=lambda path: path.name)


# --------------------------------------------------------------------------
# Iteration UI. The probe answers four questions, in priority order:
#   1. What is the per-tick token cost? (the result)
#   2. Why should I trust it? (each tick crossing shows its bracket)
#   3. How long until I know? (progress + ETA)
#   4. What did the run produce? (final results)
# Everything in this section serves one of those four. If a field doesn't
# answer one, it doesn't belong.
#
# Tokens displayed are Opus cache-write equivalent (the canonical unit used
# in the project README). We say "tokens" in the UI for brevity and define
# the unit once in the pre-flight block.
# --------------------------------------------------------------------------

SIZE_COLOR = {"lead": "green", "large": "magenta", "medium": "blue", "small": "cyan", "micro": "yellow"}


@dataclass
class _Col:
    label: str
    width: int
    align: str  # 'l' / 'r' / 'c'


# Per-iter row: iter | size | wall | tokens | →tick | util% | marker
# →tick is the running estimate of OCW remaining until the next tick boundary
# (post-call). It counts down across iters and snaps back up to ~1 tick after
# a crossing — that visual rebound is itself a useful signal that the tick
# was observed.
_COLS: list[_Col] = [
    _Col("iter",    5, "r"),
    _Col("size",    6, "l"),
    _Col("wall",    6, "r"),
    _Col("tokens", 10, "r"),
    _Col("→tick", 11, "r"),
    _Col("util",    8, "c"),
    _Col("",       18, "l"),
]
_SEP = "  "


def _pad(text: str, width: int, align: str) -> str:
    if align == "l":
        return text.ljust(width)
    if align == "r":
        return text.rjust(width)
    return text.center(width)


# --------------------------------------------------------------------------
# Pre-flight: tells the user what's about to happen, in plain terms.
# --------------------------------------------------------------------------


@dataclass
class _PreFlightSummary:
    model: str
    window: str                     # "5h" or "7d"
    util_pct_baseline: int
    target_ticks: int               # ticks we want to measure
    required_crossings: int         # crossings we need to observe (target+1 if not at boundary)
    est_tokens_per_tick: float      # OCW; the prior estimate we'll refine
    expected_wall_s: float          # rough ETA


def _render_preflight(s: _PreFlightSummary) -> Group:
    title = Rule("Probe starting", style="bold cyan", characters="═")
    window_label = "5-hour" if s.window == "5h" else "7-day"
    eta_min = s.expected_wall_s / 60.0

    rows: list[tuple[str, str]] = [
        ("Goal",     f"measure tokens per 1% tick on the {window_label} quota window"),
        ("Method",   f"observe {s.required_crossings} tick crossing{'s' if s.required_crossings != 1 else ''}; "
                     f"each crossing brackets one tick's token cost"),
        ("Starting", f"{s.util_pct_baseline}% utilization on {s.window}"),
        ("Estimate", f"~{int(round(s.est_tokens_per_tick)):,} tokens per tick (prior; will be refined)"),
        ("ETA",      f"~{eta_min:.1f} min"),
        ("Tokens",   "Opus cache-write equivalent (see README → Methodology)"),
    ]

    label_w = max(len(label) for label, _ in rows)
    lines: list[Text] = []
    for label, value in rows:
        line = Text("  ")
        line.append(_pad(label, label_w, "l"), style="bold")
        line.append("   ")
        line.append(value)
        lines.append(line)

    return Group(title, *lines)


# --------------------------------------------------------------------------
# Run: tick-bracketed iter stream. Each tick opens with a header, every iter
# is a row inside it, and the tick closes with a summary line.
# --------------------------------------------------------------------------


@dataclass
class _TickBlock:
    """One stretch of iters between two boundary observations.

    `is_leading_bracket` distinguishes the FIRST block of a run (from
    baseline up to the first crossing) from clean measurements. The leading
    block doesn't measure a tick — its starting position inside the integer
    percent range is unknown, so the cost spent within it spans an unknown
    sub-percent slice, not 1 tick. It's used as the anchor for measurement
    #1 but contributes nothing to the headline. // [LAW:single-enforcer]
    leading-bracket exclusion lives here in the UI and in metrics.go's
    capacityEstimator; both follow the same definition.
    """
    tick_num: int                      # 1-indexed within measured ticks (leading = 0)
    util_pct_at_open: int
    target_util_pct: int
    is_leading_bracket: bool = False
    units_so_far: float = 0.0
    wall_s: float = 0.0
    iter_nums: list[int] = field(default_factory=list)
    last_iter_units_before_cross: float = 0.0


def _render_tick_header(tb: _TickBlock, num_total: int) -> Text:
    line = Text("  ")
    if tb.is_leading_bracket:
        line.append("Leading bracket", style="bold dim")
        line.append("   ", style="dim")
        line.append(f"establishing anchor at {tb.target_util_pct}%  "
                    f"(not a measurement — see README → Methodology)",
                    style="dim")
        return line
    line.append(f"Measurement {tb.tick_num} of {num_total}", style="bold cyan")
    line.append("   ")
    line.append(f"crossing {tb.util_pct_at_open}% → {tb.target_util_pct}%", style="cyan")
    return line


def _render_iter_columns_header() -> Text:
    parts = [_pad(c.label, c.width, c.align) for c in _COLS]
    return Text("  " + _SEP.join(parts), style="bold dim")


def _fmt_iter_row(
    iter_num: int,
    size_name: str,
    wall_s: float,
    iter_tokens: float,                 # OCW spent on this call
    remaining_ocw: float,               # OCW estimated until next tick (post-call)
    util_pre: int,
    util_post: int,
    crossed_this_call: int,
) -> Text:
    size_c = SIZE_COLOR.get(size_name, "white")
    util_str = f"{util_pre}→{util_post}%"

    marker = Text()
    if crossed_this_call > 0:
        marker.append("← tick crossed", style="bold green")
        if crossed_this_call > 1:
            marker.append(f" (×{crossed_this_call})", style="bold yellow")

    cells: list[tuple[str | Text, str]] = [
        (_pad(f"{iter_num}",                 _COLS[0].width, _COLS[0].align), "bright_black"),
        (_pad(size_name,                     _COLS[1].width, _COLS[1].align), size_c),
        (_pad(f"{wall_s:.1f}s",              _COLS[2].width, _COLS[2].align), "bright_black"),
        (_pad(f"{int(round(iter_tokens)):,}", _COLS[3].width, _COLS[3].align), "magenta"),
        (_pad(f"{int(round(remaining_ocw)):,}", _COLS[4].width, _COLS[4].align), "cyan"),
        (_pad(util_str,                      _COLS[5].width, _COLS[5].align), "yellow"),
        (marker, ""),
    ]

    line = Text("  ")
    for i, (cell, style) in enumerate(cells):
        if i > 0:
            line.append(_SEP)
        if isinstance(cell, Text):
            line.append(cell)
        else:
            line.append(cell, style=style)
    return line


def _render_tick_close(tb: _TickBlock, crossed: int) -> Group:
    """Print the close of a tick block.

    For the leading bracket, this prints an explicit non-measurement notice —
    the cost between baseline and the first crossing spans an unknown
    sub-percent slice, NOT 1 tick. Reporting it as "tick 1: X tokens" was
    the bug that polluted both the post-flight headline and (independently)
    the proxy's running capacity estimator.

    For a measured tick, this prints the per-tick cost with its bracket: the
    last observation before the cross and the first after; the spread is the
    measurement uncertainty.
    """
    last_iter_ocw = to_ocw(tb.last_iter_units_before_cross)
    cum_after_ocw = to_ocw(tb.units_so_far)
    cum_before_ocw = max(0.0, cum_after_ocw - last_iter_ocw)

    if tb.is_leading_bracket:
        headline = Text("  ")
        headline.append("↳ ", style="dim")
        headline.append("anchor established at ", style="dim")
        headline.append(f"{tb.target_util_pct}%", style="bold")
        headline.append(f"   ({int(round(cum_after_ocw)):,} tokens spent reaching it — "
                        f"NOT counted as a measurement)", style="dim")
        return Group(headline)

    tick_tokens = to_ocw(tb.units_so_far) / max(1, crossed)
    headline = Text("  ")
    headline.append("→ ", style="bold green")
    headline.append(f"measurement {tb.tick_num}: ", style="bold")
    headline.append(f"{int(round(tick_tokens)):,} tokens", style="bold green")
    if crossed > 1:
        headline.append(f"  (averaged over {crossed} crossings — single iter spanned multiple ticks)",
                        style="bold yellow")
    headline.append(f"   in {tb.wall_s:.1f}s")

    detail = Text("    ", style="dim")
    detail.append(f"bracket: {int(round(cum_before_ocw)):,} before last call → "
                  f"{int(round(cum_after_ocw)):,} after  "
                  f"(uncertainty ≈ {int(round(last_iter_ocw)):,} tokens)")

    return Group(headline, detail)


# --------------------------------------------------------------------------
# Live HUD: progress + running estimate + ETA. Three short lines.
# --------------------------------------------------------------------------


@dataclass
class _FooterState:
    window: str
    crossings: int = 0
    need: int = 0
    cum_wall_s: float = 0.0
    in_flight: bool = False
    in_flight_size: str = ""
    target_reached: bool = False
    # Running estimate, refined as ticks cross:
    est_tokens_per_tick: float = 0.0   # OCW
    samples: int = 0                   # number of crossed ticks contributing
    spread_pct: float = 0.0            # |max - min| / mid across samples


class _IterationFooter:
    def __init__(self, state: _FooterState) -> None:
        self.state = state
        self._spinner = Spinner("dots", style="cyan")

    def update(self, **kwargs: object) -> None:
        for k, v in kwargs.items():
            setattr(self.state, k, v)

    def __rich__(self) -> Group:
        s = self.state

        # Line 1: status / progress
        line1 = Text("  ")
        if s.target_reached:
            line1.append("✓ ", style="bold green")
            line1.append(f"complete — {s.crossings} of {s.need} tick crossings observed", style="bold green")
        elif s.in_flight:
            line1.append(self._spinner.render(time.monotonic()))
            line1.append(f" tick {s.crossings + 1} of {s.need}", style="cyan")
            line1.append(f"  ·  sending {s.in_flight_size} prompt", style="dim")
        else:
            line1.append(f"tick {s.crossings} of {s.need} observed", style="cyan")

        # Line 2: the running answer + trust signal
        line2 = Text("  ")
        if s.samples == 0:
            line2.append("running estimate: ", style="dim")
            line2.append("(no ticks crossed yet)", style="dim")
        else:
            line2.append("running estimate: ", style="dim")
            line2.append(f"{int(round(s.est_tokens_per_tick)):,} tokens / tick", style="bold")
            line2.append(f"  (from {s.samples} crossing{'s' if s.samples != 1 else ''}", style="dim")
            if s.samples >= 2:
                line2.append(f", spread ±{s.spread_pct:.1f}%", style="dim")
            line2.append(")", style="dim")

        # Line 3: ETA
        line3 = Text("  ", style="dim")
        elapsed = s.cum_wall_s
        if s.crossings > 0 and not s.target_reached:
            per_tick_s = elapsed / s.crossings
            remaining_ticks = max(0, s.need - s.crossings)
            eta_s = per_tick_s * remaining_ticks
            line3.append(f"elapsed {elapsed:.0f}s  ·  ETA ~{eta_s:.0f}s")
        else:
            line3.append(f"elapsed {elapsed:.0f}s")

        return Group(Rule(style="bright_black"), line1, line2, line3)


# --------------------------------------------------------------------------
# Post-flight: HEADLINE result first, then per-tick bracket table.
# --------------------------------------------------------------------------


@dataclass
class _PerTickSummary:
    tick_num: int                      # measurement number (1-indexed); 0 = leading bracket
    util_pre: int
    util_post: int
    units: float                       # input-equivalent tokens consumed in this block
    last_iter_units_before_cross: float
    wall_s: float
    iter_nums: list[int]
    crossed: int                       # 0 = in flight, ≥1 = crossings observed
    is_leading_bracket: bool = False   # True for the first block; never a measurement


def _render_postflight(
    pre: _PreFlightSummary,
    per_tick: list[_PerTickSummary],
    total_wall_s: float,
    interrupted: bool,
    previous: _PreviousRunComparison | None = None,
) -> Group:
    """Result panel.

    The post-flight headline reports per-tick token cost averaged across CLEAN
    measurements only — leading-bracket blocks (no clean starting position) and
    in-flight blocks (no closing crossing) are surfaced for transparency but
    excluded from the average. Same rule report.py applies via measured_ticks.

    `previous` is rendered as a comparison line beneath the headline when both
    sides have a clean measurement; with no previous run the line is omitted.
    """
    measured = [t for t in per_tick if t.crossed > 0 and not t.is_leading_bracket]
    leading = [t for t in per_tick if t.is_leading_bracket]
    in_flight = [t for t in per_tick if t.crossed == 0 and not t.is_leading_bracket]

    title = Rule("Result", style="bold cyan", characters="═")

    if not measured:
        msg = Text("  ")
        msg.append("Insufficient data — no clean measurements", style="bold yellow")
        msg.append("\n  ", style="dim")
        msg.append("A clean measurement requires two observed crossings (the first "
                   "establishes the anchor, subsequent crossings each measure one tick).",
                   style="dim")
        msg.append("\n  ", style="dim")
        if leading:
            msg.append(f"This run anchored at {leading[0].util_post}% but no further "
                       "crossings were observed before it ended.", style="dim")
        else:
            msg.append("No crossings were observed before the run ended.", style="dim")
        return Group(title, msg)

    per_tick_tokens = [to_ocw(t.units) / t.crossed for t in measured]
    mid = sum(per_tick_tokens) / len(per_tick_tokens)
    lo = min(per_tick_tokens)
    hi = max(per_tick_tokens)
    spread_pct = (hi - lo) / mid * 100 if mid > 0 else 0.0

    headline = Text("  ")
    headline.append(f"{int(round(mid)):,}", style="bold green")
    headline.append(" tokens per 1% tick", style="bold")
    headline.append(f"   ({pre.window} window, {len(measured)} clean measurement"
                    f"{'s' if len(measured) != 1 else ''})", style="dim")

    compare_line: Text | None = None
    if previous is not None and previous.ocw_per_tick > 0:
        delta = mid - previous.ocw_per_tick
        pct = (delta / previous.ocw_per_tick) * 100
        abs_pct = abs(pct)
        if abs_pct < 1.0:
            delta_style = "green"
        elif abs_pct < 5.0:
            delta_style = "yellow"
        else:
            delta_style = "red"
        arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "·")
        compare_line = Text("  ")
        compare_line.append(f"{arrow} {abs_pct:.1f}%", style=f"bold {delta_style}")
        compare_line.append(" vs previous run", style="dim")
        compare_line.append(
            f"   (was {int(round(previous.ocw_per_tick)):,} — "
            f"{previous.run_name}, {previous.clean_ticks} measurement"
            f"{'s' if previous.clean_ticks != 1 else ''})",
            style="dim",
        )

    bracket = Text("  ")
    bracket.append("range across measurements: ", style="dim")
    bracket.append(f"{int(round(lo)):,} — {int(round(hi)):,}")
    bracket.append(f"   (spread {spread_pct:.2f}%)", style="dim")

    full_quota = mid * 100
    quota_line = Text("  ")
    quota_line.append("→ implied full-window quota: ", style="dim")
    quota_line.append(f"{int(round(full_quota)):,} tokens", style="bold")

    table_title = Text("\n  Measurements (bracket data):", style="bold")
    cols = [("#", 3, "l"), ("util", 11, "l"), ("tokens", 12, "r"),
            ("uncertainty", 18, "r"), ("wall", 9, "r"), ("iters", 7, "r")]
    sep = "   "
    header = Text("    ")
    header.append(sep.join(_pad(lbl, w, a) for lbl, w, a in cols), style="bold dim")

    rows: list[Text] = [header]
    for t in measured:
        per_tick_t = to_ocw(t.units) / t.crossed
        last_iter_ocw = to_ocw(t.last_iter_units_before_cross)
        uncertainty_pct = (last_iter_ocw / per_tick_t * 100) if per_tick_t > 0 else 0.0

        util_s = f"{t.util_pre}→{t.util_post}%"
        if t.crossed > 1:
            util_s += f" (×{t.crossed})"
        cells = [
            _pad(str(t.tick_num),                              cols[0][1], cols[0][2]),
            _pad(util_s,                                       cols[1][1], cols[1][2]),
            _pad(f"{int(round(per_tick_t)):,}",                cols[2][1], cols[2][2]),
            _pad(f"±{int(round(last_iter_ocw)):,} ({uncertainty_pct:.1f}%)",
                                                               cols[3][1], cols[3][2]),
            _pad(f"{t.wall_s:.1f}s",                           cols[4][1], cols[4][2]),
            _pad(str(len(t.iter_nums)),                        cols[5][1], cols[5][2]),
        ]
        row = Text("    ")
        row.append(sep.join(cells))
        rows.append(row)

    notes: list[Text] = []
    if leading:
        lb = leading[0]
        ln = Text("\n  ", style="dim")
        ln.append("excluded (leading bracket): ", style="bold dim")
        ln.append(f"{int(round(to_ocw(lb.units))):,} tokens spent reaching the first "
                  f"crossing at {lb.util_post}%, across {len(lb.iter_nums)} iter"
                  f"{'s' if len(lb.iter_nums) != 1 else ''}. "
                  "Internal starting position unknown — not a measurement.",
                  style="dim")
        notes.append(ln)
    if in_flight:
        ifb = in_flight[0]
        n = Text("\n  ", style="dim")
        n.append("excluded (in flight): ", style="bold dim")
        n.append(f"{len(ifb.iter_nums)} iter(s) past the last crossing — "
                 "no closing crossing observed.",
                 style="dim")
        notes.append(n)
    if interrupted:
        n = Text("  ", style="dim")
        n.append("note: ", style="bold yellow")
        n.append("run was interrupted before reaching the target.")
        notes.append(n)

    summary_meta = Text("\n  ", style="dim")
    summary_meta.append(f"target {pre.required_crossings} crossings  ·  "
                        f"observed {len(measured) + len(leading)} crossing"
                        f"{'s' if len(measured) + len(leading) != 1 else ''}  ·  "
                        f"clean measurements {len(measured)}  ·  "
                        f"total wall {total_wall_s:.1f}s")

    parts: list[object] = [title, headline]
    if compare_line is not None:
        parts.append(compare_line)
    parts.extend([bracket, quota_line, summary_meta, table_title, *rows, *notes])
    return Group(*parts)


def main() -> None:
    args = parse_args()
    window = args.window
    util_key = f"util_{window}"
    other_window = "7d" if window == "5h" else "5h"

    metrics_url = os.environ.get("METRICS_URL", "http://localhost:9481/metrics")
    data_dir = Path(os.environ.get("DATA_DIR", str(Path.home() / ".local" / "cc-nerf-buster")))
    model = os.environ.get("MODEL", "claude-opus-4-7")
    target_env = f"TARGET_{window.upper()}_TICKS"
    estimate_env = f"ESTIMATE_{window.upper()}_INPUT_EQUIV_PER_TICK"
    target_ticks = int(os.environ.get(target_env, str(DEFAULT_TARGET_TICKS[window])))
    dry_iterations = int(os.environ.get("DRY_ITERATIONS", "3"))

    if args.continue_latest:
        args.resume = str(resolve_continue_run(data_dir, window, args.dry_run))

    script_dir = Path(__file__).resolve().parent
    ts = run_ts()
    run_dir = Path(args.resume).expanduser() if args.resume else data_dir / "probe-runs" / (f"dryrun-{ts}" if args.dry_run else ts)
    (run_dir / "raw-metrics").mkdir(parents=True, exist_ok=True)
    (run_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (run_dir / "claude-output").mkdir(parents=True, exist_ok=True)

    # probe-config: persistent Claude config dir for all probe invocations.
    # Contains credentials (from one-time login) but no CLAUDE.md, so the
    # global ~/.claude/CLAUDE.md is never injected — per-call overhead ≈ 0.
    # Also holds the empty MCP config (fixed content, not a run artifact).
    probe_config_dir = data_dir / "probe-config"
    if not args.dry_run:
        probe_config_dir.mkdir(parents=True, exist_ok=True)
        creds_path = probe_config_dir / "credentials.json"
        if not creds_path.exists():
            die(
                f"probe-config not authenticated.\n"
                f"Run once to log in, then Ctrl-C to exit:\n"
                f"\n"
                f"  CLAUDE_CONFIG_DIR={probe_config_dir} claude\n"
                f"\n"
                f"The probe will reuse these credentials for all calls."
            )
    # Empty MCP config: persistent in probe-config, not per-run. Format must
    # include the mcpServers key for --strict-mcp-config to accept it.
    empty_mcp_config_path = probe_config_dir / "empty-mcp.json"
    if not args.dry_run:
        empty_mcp_config_path.write_text('{"mcpServers":{}}\n')

    log(f"target: {target_ticks} ticks on the {window} window")
    if args.dry_run:
        log(f"DRY RUN: 'echo' replaces 'claude'; will stop after {dry_iterations} iterations. No API calls will be made.")

    try:
        fetch_metrics(metrics_url)
    except Exception as exc:
        die(f"metrics endpoint {metrics_url} not reachable: {exc}")
    if not args.dry_run and not (data_dir / "ca.crt").exists():
        die(f"missing CA cert at {data_dir / 'ca.crt'}; run 'just install' first")

    copy_with_hashes(run_dir, script_dir)

    if args.resume:
        manifest = json.loads((run_dir / "manifest.json").read_text())
        if manifest.get("window") != window:
            die(f"resume mismatch: run was --window={manifest.get('window')!r}, you passed --window={window!r}")
        baseline = manifest["baseline"]
        baseline_zero = bool(manifest["baseline_zero"])
        need = int(manifest["required_crossings"])
        est_units_per_tick = float(manifest["estimated_input_equiv_tokens_per_tick"])
        current, baseline, next_iter, used_units_since_tick, msgs_since_tick = load_resume_state(run_dir, window)
        log(
            f"resuming run: {run_dir} "
            f"(next_iter={next_iter:03d}, used_since_tick={used_units_since_tick:.1f}, "
            f"msgs_in_current_tick={msgs_since_tick})"
        )
    else:
        log("fetching baseline snapshot")
        baseline = snapshot_metrics(run_dir, metrics_url, "000-baseline", model)
        baseline_zero = is_zero_util(baseline[util_key])
        need = target_ticks + 1 - int(baseline_zero)
        msgs_since_tick = 0
        # // [LAW:one-source-of-truth] the measured per-tick value (from a real
        # probe run on this account) is the initial estimate; the proxy's
        # capacity_usd guess was off by ~7x in practice. Refinement from
        # observed crossings (in the iteration loop) replaces this as soon
        # as we have one real data point.
        est_units_per_tick = float(os.environ.get(estimate_env, str(DEFAULT_INPUT_EQUIV_PER_TICK[window])))

        manifest = {
            "started": ts,
            "metrics_url": metrics_url,
            "model": model,
            "window": window,
            "target_ticks": target_ticks,
            "required_crossings": need,
            "baseline_zero": baseline_zero,
            "dry_run": args.dry_run,
            "dry_iterations": dry_iterations,
            "baseline": baseline,
            "prompt_stats": PROMPT_STATS,
            "system_prompt": "",
            "estimated_input_equiv_tokens_per_tick": est_units_per_tick,
        }
        (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

        current = baseline
        used_units_since_tick = 0.0
        next_iter = 1

    # Cumulative measurements drive estimate refinement: per-tick = total
    # input-equiv consumed / total ticks crossed, replacing the initial
    # estimate with ground truth as soon as we have any.
    observed_units = 0.0
    observed_ticks = 0

    cmd_preview = (
        f"CLAUDE_CONFIG_DIR={probe_config_dir} "
        f"claude -p --model {model} --system-prompt '' --no-session-persistence "
        f"--tools '' --mcp-config {empty_mcp_config_path.name} --strict-mcp-config "
        f"--no-chrome --effort low --disable-slash-commands --"
        if not args.dry_run
        else "python3 -c 'echo'"
    )
    log(f"model: {model}")
    log(f"proxy scope: organization {baseline['org'] or '(none yet)'}, upstream {baseline['upstream'] or '(none yet)'}")
    log(
        f"baseline utilization: "
        f"{window} window {int(baseline[util_key]*100+1e-9)}% (driven), "
        f"{other_window} window {int(baseline[f'util_{other_window}']*100+1e-9)}% (informational)"
    )
    log(
        f"initial estimate of input-equivalent tokens per 1% tick "
        f"({window}): ≈ {est_units_per_tick:,.0f}"
    )
    log(f"command run per iteration: {cmd_preview} <prompt>")

    ui_console = Console(file=sys.stderr, force_terminal=sys.stderr.isatty())

    # ─── Pre-flight ───────────────────────────────────────────────────────
    util_pct_baseline = int(baseline[util_key] * 100 + 1e-9)
    # Wall-clock estimate: rough ~4s per message, ~5 messages per tick in
    # healthy operation. This is a hint for the user; refined as the run
    # progresses by using observed wall-time-per-tick.
    expected_wall_s = float(need * 5 * 4.0)

    pre = _PreFlightSummary(
        model=model,
        window=window,
        util_pct_baseline=util_pct_baseline,
        target_ticks=target_ticks,
        required_crossings=need,
        est_tokens_per_tick=to_ocw(est_units_per_tick),
        expected_wall_s=expected_wall_s,
    )
    ui_console.print(_render_preflight(pre))

    # Tick-block tracking. Each entry in closed_ticks is one block of iters
    # between two boundary observations. The FIRST block is the leading
    # bracket — its starting position inside the integer percent is unknown,
    # so it cannot be a measurement; it serves only as the anchor for the
    # next crossing's measurement. Subsequent blocks are clean per-tick
    # measurements numbered 1..N.
    # // [LAW:single-enforcer] the leading-bracket exclusion enforced here
    # mirrors metrics.go's capacityEstimator and report.py's measured_ticks.
    closed_ticks: list[_PerTickSummary] = []
    measurements_done = 0  # count of clean measurements observed so far

    footer = _IterationFooter(_FooterState(
        window=window,
        crossings=tick_delta(current[util_key], baseline[util_key]),
        need=need,
        est_tokens_per_tick=0.0,  # no measurement yet; estimate appears once warmed
    ))

    interrupted = False

    def handle_interrupt(_signum: int, _frame: object) -> None:
        nonlocal interrupted
        interrupted = True
        log("interrupt received; will stop after current iteration")

    signal.signal(signal.SIGINT, handle_interrupt)
    signal.signal(signal.SIGTERM, handle_interrupt)

    ticks_seen = tick_delta(current[util_key], baseline[util_key])
    if ticks_seen >= need:
        log(f"resume target already reached: {window} {ticks_seen}/{need}")
        ui_console.print(Text(""))
        ui_console.print(Text(
            "  No tick blocks observed in this invocation "
            "(resumed into already-complete state).",
            style="dim",
        ))
        run_report(script_dir, run_dir)
        log(f"run complete: {run_dir}")
        return

    total_msgs = 0
    total_units = 0.0

    # The first block is always the leading bracket (starting position
    # inside the integer percent is unknown — see _TickBlock docstring).
    # tick_num=0 reserves measurement numbers 1..N for clean ticks.
    starting_baseline_ticks = tick_delta(current[util_key], baseline[util_key])
    active_tick = _TickBlock(
        tick_num=0,
        util_pct_at_open=util_pct_baseline + starting_baseline_ticks,
        target_util_pct=util_pct_baseline + starting_baseline_ticks + 1,
        is_leading_bracket=True,
        units_so_far=used_units_since_tick,
    )

    # // [LAW:single-enforcer] the Live block owns the persistent footer for
    # the entire iteration loop. Inside this `with`, calls to ui_console.print
    # emit above the live region; early `return`s exit Live cleanly via __exit__.
    with Live(footer, console=ui_console, refresh_per_second=10, transient=False) as live:
        ui_console.print(Text(""))
        # The number of clean measurements is need-1 (first crossing is the anchor).
        target_measurements = max(0, need - 1)
        ui_console.print(_render_tick_header(active_tick, target_measurements))
        ui_console.print(_render_iter_columns_header())

        for iter_num in range(next_iter, 10_000):
            if interrupted:
                break

            n = f"{iter_num:03d}"
            ticks_seen = tick_delta(current[util_key], baseline[util_key])
            size_name, rem = choose_prompt_size(
                ticks_seen, need, est_units_per_tick, used_units_since_tick,
                is_leading_bracket=active_tick.is_leading_bracket,
            )

            # In-flight: spinner + size label in the footer while claude runs.
            footer.update(in_flight=True, in_flight_size=size_name)
            live.refresh()

            # Timestamp at the START so each call has a unique prefix and pays
            # full cache_write cost — that's how a `large` prompt actually consumes
            # large quota per call. Caching the corpus would let large cache_read
            # at ~10% the cost, defeating its purpose. Micro stays below the cache
            # threshold so it skips caching naturally and is sized dynamically to
            # the remaining distance to the next tick boundary.
            # Target HALF the remaining distance so micro just barely doesn't
            # cross — binary-search the boundary. Each non-crossing iter halves
            # the gap; eventually the smallest meaningful prompt (~80 input-equiv
            # output floor) finally tips us over with minimal overshoot.
            prompt_corpus = build_prompt(
                size_name,
                micro_target_input_equiv=rem * 0.5,
                lead_est_units_per_tick=est_units_per_tick,
            )
            prompt = f"Probe timestamp: {utc_now()}\n" + prompt_corpus
            prompt_path = run_dir / "prompts" / f"{n}.txt"
            prompt_path.write_text(prompt)
            output_path = run_dir / "claude-output" / f"{n}.txt"

            cmd_name = "python3" if args.dry_run else "claude"
            if not args.dry_run:
                # [LAW:single-enforcer] `--` is the single boundary that stops the
                # variadic `--tools` option from consuming the positional prompt.
                # CLAUDE_CONFIG_DIR (set in claude_env) eliminates per-call
                # overhead by using a config dir with no CLAUDE.md. The flags
                # below further strip tools, session state, MCP, Chrome, and
                # effort so the only cost is the prompt itself.
                cmd = [
                    cmd_name, "-p",
                    "--model", model,
                    "--system-prompt", "",
                    "--no-session-persistence",
                    "--tools", "",
                    "--mcp-config", str(empty_mcp_config_path),
                    "--strict-mcp-config",
                    "--no-chrome",
                    "--effort", "low",
                    "--disable-slash-commands",
                    "--", prompt,
                ]
            else:
                cmd = [cmd_name, "-c", "import sys; print(sys.argv[1])", prompt]

            start = time.time_ns()
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=claude_env(data_dir) if not args.dry_run else None,
            )
            stdout, _ = proc.communicate()
            wall_ms = (time.time_ns() - start) // 1_000_000
            completed = subprocess.CompletedProcess(cmd, proc.returncode, stdout)
            output_path.write_text(completed.stdout)
            output_summary = summarize_output(completed.stdout)

            if interrupted:
                log("interrupt received during iteration; skipping post-call metrics snapshot and exiting")
                # Fall through to postflight + run_report so the integrated
                # Result panel (with comparison) renders for this path too.
                break

            try:
                snap = snapshot_metrics(run_dir, metrics_url, f"{n}-after", model)
            except Exception as exc:
                log(f"metrics endpoint unreachable after iteration {iter_num} ({exc}); proxy likely shut down — running report on data captured so far")
                interrupted = True
                break
            usage = {
                "requests": snap["model_requests"] - current["model_requests"],
                "input": snap["model_input_tokens"] - current["model_input_tokens"],
                "output": snap["model_output_tokens"] - current["model_output_tokens"],
                "cache_create": snap["model_cache_creation_input_tokens"] - current["model_cache_creation_input_tokens"],
                "cache_read": snap["model_cache_read_input_tokens"] - current["model_cache_read_input_tokens"],
            }
            priced_iter_units = quota_input_equivalent_tokens(usage)
            iter_units = priced_iter_units
            prev_ticks = tick_delta(current[util_key], baseline[util_key])
            new_ticks = tick_delta(snap[util_key], baseline[util_key])
            crossed = max(0, new_ticks - prev_ticks)

            # Bookkeeping: budget consumed AND per-tick state. Per-tick state
            # carries the *full* (pre-rollover) units; if we cross, the close
            # snapshot uses these values, and only AFTER the snapshot do we
            # subtract est_per_tick × crossed for the next tick's overhang.
            used_units_since_tick += iter_units
            observed_units += iter_units
            msgs_since_tick += 1
            total_msgs += 1
            total_units += iter_units

            active_tick.units_so_far = used_units_since_tick
            active_tick.wall_s += wall_ms / 1000.0
            active_tick.iter_nums.append(iter_num)
            active_tick.last_iter_units_before_cross = iter_units

            if interrupted:
                run_report(script_dir, run_dir)
                log(f"resume with: {resume_command_for_run(window, run_dir)}")
                return

            iteration = {
                "iter": iter_num,
                "ts": utc_now(),
                "prompt_file": str(prompt_path),
                "output_file": str(output_path),
                "prompt_size": size_name,
                "prompt_chars": len(prompt_corpus),
                "prompt_words": len(prompt_corpus.split()),
                "window": window,
                "estimated_remaining_input_equiv_tokens_before_call": rem,
                "exit_code": completed.returncode,
                "wall_ms": wall_ms,
                "requests": usage["requests"],
                "input_tokens": usage["input"],
                "output_tokens": usage["output"],
                "cache_creation_input_tokens": usage["cache_create"],
                "cache_read_input_tokens": usage["cache_read"],
                "priced_input_equivalent_tokens": priced_iter_units,
                "input_equivalent_tokens": iter_units,
            }
            with (run_dir / "iterations.jsonl").open("a") as f:
                f.write(json.dumps(iteration) + "\n")

            if completed.returncode != 0 and not args.dry_run:
                log(f"#{n} claude exited {completed.returncode}: {output_summary}")

            if usage["requests"] == 0 and not args.dry_run:
                fatal_reason = fatal_output_reason(completed.stdout)
                # // [LAW:single-enforcer] local CLI failures should be diagnosed at
                # the probe boundary once, instead of silently leaking as zero-usage iterations.
                if fatal_reason is not None:
                    die(f"claude failed locally before any proxied request: {fatal_reason}")

            ticks_now = tick_delta(snap[util_key], baseline[util_key])
            # // [LAW:one-source-of-truth] Anthropic reports util as integer percent;
            # never display fractional percentages we didn't actually measure.
            util_pct_pre = int(current[util_key] * 100 + 1e-9)
            util_pct = int(snap[util_key] * 100 + 1e-9)

            # Render the iter row above the live footer.
            #
            # The remaining-OCW value shown is computed POST-call, against the
            # current best estimate. Pre-rebase (before any crossing rollover)
            # so that on a crossing iter the displayed value goes to 0 and the
            # "← tick crossed" marker explains the rebound; the very next iter
            # then shows ~one full tick remaining again, making the cadence
            # legible at a glance.
            remaining_ocw_pre_rebase = to_ocw(
                max(0.0, est_units_per_tick - used_units_since_tick)
            )
            ui_console.print(
                _fmt_iter_row(
                    iter_num=iter_num,
                    size_name=size_name,
                    wall_s=wall_ms / 1000.0,
                    iter_tokens=to_ocw(iter_units),
                    remaining_ocw=remaining_ocw_pre_rebase,
                    util_pre=util_pct_pre,
                    util_post=util_pct,
                    crossed_this_call=crossed,
                ),
            )

            if crossed > 0:
                # Close the active block. The leading bracket (first block) is
                # only the anchor; subsequent blocks are clean per-tick measurements.
                closed_ticks.append(_PerTickSummary(
                    tick_num=active_tick.tick_num,
                    util_pre=active_tick.util_pct_at_open,
                    util_post=util_pct,
                    units=active_tick.units_so_far,
                    last_iter_units_before_cross=active_tick.last_iter_units_before_cross,
                    wall_s=active_tick.wall_s,
                    iter_nums=list(active_tick.iter_nums),
                    crossed=crossed,
                    is_leading_bracket=active_tick.is_leading_bracket,
                ))
                ui_console.print(_render_tick_close(active_tick, crossed))

                # Refine est_units_per_tick from CLEAN measurements only — the
                # leading bracket's cost includes an unknown sub-percent slice
                # and must not contribute to the running estimate.
                # // [LAW:single-enforcer] same exclusion rule as metrics.go.
                if not active_tick.is_leading_bracket:
                    measurements_done += crossed
                    measured_blocks = [t for t in closed_ticks
                                       if not t.is_leading_bracket and t.crossed > 0]
                    if measured_blocks:
                        observed_units_clean = sum(t.units for t in measured_blocks)
                        observed_ticks_clean = sum(t.crossed for t in measured_blocks)
                        est_units_per_tick = observed_units_clean / observed_ticks_clean
                used_units_since_tick = max(0.0, used_units_since_tick - est_units_per_tick * crossed)
                msgs_since_tick = 0

                if ticks_now < need:
                    next_tick_num = measurements_done + 1
                    active_tick = _TickBlock(
                        tick_num=next_tick_num,
                        util_pct_at_open=util_pct,
                        target_util_pct=util_pct + 1,
                        is_leading_bracket=False,
                        units_so_far=used_units_since_tick,
                    )
                    ui_console.print(Text(""))
                    ui_console.print(_render_tick_header(active_tick, target_measurements))
                    ui_console.print(_render_iter_columns_header())

            # Running estimate for the live footer: clean measurements only.
            measured_per_tick_tokens = [to_ocw(t.units) / t.crossed for t in closed_ticks
                                        if not t.is_leading_bracket and t.crossed > 0]
            if measured_per_tick_tokens:
                est_mid = sum(measured_per_tick_tokens) / len(measured_per_tick_tokens)
                spread = (max(measured_per_tick_tokens) - min(measured_per_tick_tokens)) / est_mid * 100 \
                    if est_mid > 0 else 0.0
            else:
                est_mid = 0.0
                spread = 0.0

            footer.update(
                crossings=ticks_now,
                cum_wall_s=footer.state.cum_wall_s + wall_ms / 1000.0,
                est_tokens_per_tick=est_mid,
                samples=len(measured_per_tick_tokens),
                spread_pct=spread,
                in_flight=False,
                target_reached=ticks_now >= need,
            )

            current = snap
            ticks_seen = tick_delta(current[util_key], baseline[util_key])

            if args.dry_run:
                if iter_num >= dry_iterations:
                    log(f"DRY RUN: completed {iter_num} iteration(s); stopping (observed {ticks_seen})")
                    break
                continue

            if ticks_seen >= need:
                log(f"target reached ({window}): observed {ticks_seen} crossings (need {need}) → {target_ticks} clean tick(s) across {iter_num} iterations")
                break

    # Live region with transient=False leaves the final footer rendered in
    # place but does not emit a trailing newline; without this, the next
    # log() lands on the same visual line as the footer's last char.
    print(file=sys.stderr)

    # If the active block didn't close (interrupt or DRY RUN ended mid-tick),
    # surface its iters in post-flight as in-flight (crossed=0). It's never
    # promoted to a measurement; the post-flight handler shows it as excluded.
    if active_tick.iter_nums:
        already_recorded = closed_ticks and (
            closed_ticks[-1].is_leading_bracket == active_tick.is_leading_bracket
            and closed_ticks[-1].tick_num == active_tick.tick_num
        )
        if not already_recorded:
            closed_ticks.append(_PerTickSummary(
                tick_num=active_tick.tick_num,
                util_pre=active_tick.util_pct_at_open,
                util_post=active_tick.util_pct_at_open,
                units=active_tick.units_so_far,
                last_iter_units_before_cross=active_tick.last_iter_units_before_cross,
                wall_s=active_tick.wall_s,
                iter_nums=list(active_tick.iter_nums),
                crossed=0,
                is_leading_bracket=active_tick.is_leading_bracket,
            ))

    # ─── Post-flight ──────────────────────────────────────────────────────
    previous = compute_previous_comparison(data_dir, run_dir, window)
    ui_console.print(_render_postflight(
        pre=pre,
        per_tick=closed_ticks,
        total_wall_s=footer.state.cum_wall_s,
        interrupted=interrupted,
        previous=previous,
    ))

    run_report(script_dir, run_dir)

    if interrupted:
        log(f"resume with: {resume_command_for_run(window, run_dir)}")
        return

    log(f"run complete: {run_dir}")


if __name__ == "__main__":
    main()
