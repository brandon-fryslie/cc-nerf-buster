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
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen


PROMPT_HEADER = """Read the operational notes below and reply with one short sentence:
The notes describe normal service activity.
Do not add anything else.
"""

# Sizes calibrated against measured cost on Opus 4.7 (~0.75 input-equiv per
# prompt char, dominated by cache_write × 2). At ~550k input-equiv per 1%
# tick: large lands ~75% of a tick, medium ~3%, small ~1%. Micro is dynamic.
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


def bold(s: object) -> str: return _c(s, "1")
def dim(s: object) -> str: return _c(s, "2")
def red(s: object) -> str: return _c(s, "31")
def green(s: object) -> str: return _c(s, "32")
def yellow(s: object) -> str: return _c(s, "33")
def blue(s: object) -> str: return _c(s, "34")
def magenta(s: object) -> str: return _c(s, "35")
def cyan(s: object) -> str: return _c(s, "36")


def log(msg: str) -> None:
    ts = datetime.now(UTC).strftime("%H:%M:%S")
    print(f"{dim(f'[{ts}]')} {msg}", file=sys.stderr)


def log_raw(msg: str) -> None:
    print(msg, file=sys.stderr)


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
) -> tuple[str, float]:
    # // [LAW:dataflow-not-control-flow] every iteration follows the same
    # choose-build-send-measure sequence; only the prompt size value changes.
    if ticks_seen >= need or est_units_per_tick <= 0:
        return ("large", 0.0)
    remaining_units = max(0.0, est_units_per_tick - used_units_since_tick)
    remaining_ratio = remaining_units / est_units_per_tick
    if remaining_ratio > 0.50:
        size = "large"
    elif remaining_ratio > 0.20:
        size = "medium"
    elif remaining_ratio > 0.08:
        size = "small"
    else:
        size = "micro"
    return size, remaining_units


def build_prompt(size_name: str, micro_target_input_equiv: float = 0.0) -> str:
    if size_name == "micro":
        return build_micro_prompt(micro_target_input_equiv)
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
    subprocess.run(
        [sys.executable, str(script_dir / "report.py"), "--print-bounds", str(run_dir)],
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


def fmt_delta(curr: float | None, prev: float | None, unit: str = "") -> str:
    if curr is None or prev is None or prev == 0:
        return dim("(no comparison)")
    delta = curr - prev
    pct = (delta / prev) * 100
    arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "·")
    sign = "+" if delta >= 0 else ""
    body = f"{arrow} {sign}{delta:,.0f}{unit} ({sign}{pct:.1f}%)"
    if abs(pct) < 1.0:
        return green(body)
    if abs(pct) < 5.0:
        return yellow(body)
    return red(body)


def print_comparison(run_dir: Path, data_dir: Path, window: str) -> None:
    cur_path = run_dir / "bounds.json"
    if not cur_path.exists():
        return
    prev_run = find_previous_run(data_dir, run_dir, window)
    log_raw("")
    log_raw(bold(f"=== Comparison to previous {window} run ==="))
    if prev_run is None:
        log_raw(dim(f"  (no previous non-dry-run with bounds.json found for window {window})"))
        return
    cur = json.loads(cur_path.read_text())
    prev = json.loads((prev_run / "bounds.json").read_text())
    log_raw(f"  previous run directory: {dim(prev_run.name)}")
    cw = cur["windows"].get(window, {})
    pw = prev["windows"].get(window, {})
    cur_ticks = cw.get("clean_measured_ticks", 0)
    prev_ticks = pw.get("clean_measured_ticks", 0)
    cur_usd = (cw.get("weighted_usd") or {}).get("midpoint")
    prev_usd = (pw.get("weighted_usd") or {}).get("midpoint")
    cur_opus = ((cw.get("tokens") or {}).get("midpoint") or {}).get("opus", {}).get("input_per_tick")
    prev_opus = ((pw.get("tokens") or {}).get("midpoint") or {}).get("opus", {}).get("input_per_tick")
    log_raw(f"  {bold(window)} window:")
    log_raw(f"    clean measured ticks: {cur_ticks} this run, {prev_ticks} previous run")
    if cur_usd is not None and prev_usd is not None:
        log_raw(
            f"    weighted-USD per 1% tick: {bold(f'{cur_usd:.2f}')} this run, "
            f"{prev_usd:.2f} previous run    change: {fmt_delta(cur_usd, prev_usd, ' USD')}"
        )
    if cur_opus is not None and prev_opus is not None:
        log_raw(
            f"    Opus input tokens per 1% tick: {bold(f'{cur_opus:,}')} this run, "
            f"{prev_opus:,} previous run    change: {fmt_delta(cur_opus, prev_opus, ' tokens')}"
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


def load_resume_state(run_dir: Path, window: str) -> tuple[dict, dict, int, float]:
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
        if crossed > 0 and est_units_per_tick > 0:
            used_units_since_tick = max(0.0, used_units_since_tick - est_units_per_tick * crossed)

        current = snap

    next_iter = 1
    if iterations:
        next_iter = int(iterations[-1]["iter"]) + 1

    return current, baseline, next_iter, used_units_since_tick


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
        current, baseline, next_iter, used_units_since_tick = load_resume_state(run_dir, window)
        log(
            f"resuming run: {run_dir} "
            f"(next_iter={next_iter:03d}, used_since_tick={used_units_since_tick:.1f})"
        )
    else:
        log("fetching baseline snapshot")
        baseline = snapshot_metrics(run_dir, metrics_url, "000-baseline", model)
        baseline_zero = is_zero_util(baseline[util_key])
        need = target_ticks + 1 - int(baseline_zero)
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
        f"claude -p --model {model} --system-prompt '' --no-session-persistence --tools '' --"
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
        log("running report.py")
        run_report(script_dir, run_dir)
        log(f"run complete: {run_dir}")
        print(run_dir)
        return

    for iter_num in range(next_iter, 10_000):
        if interrupted:
            break

        n = f"{iter_num:03d}"
        ticks_seen = tick_delta(current[util_key], baseline[util_key])
        size_name, rem = choose_prompt_size(
            ticks_seen, need, est_units_per_tick, used_units_since_tick
        )

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
        prompt_corpus = build_prompt(size_name, micro_target_input_equiv=rem * 0.5)
        prompt = f"Probe timestamp: {utc_now()}\n" + prompt_corpus
        prompt_path = run_dir / "prompts" / f"{n}.txt"
        prompt_path.write_text(prompt)
        output_path = run_dir / "claude-output" / f"{n}.txt"

        cmd_name = "python3" if args.dry_run else "claude"
        if not args.dry_run:
            # [LAW:single-enforcer] `--` is the single boundary that stops the
            # variadic `--tools` option from consuming the positional prompt.
            cmd = [cmd_name, "-p", "--model", model, "--system-prompt", "", "--no-session-persistence", "--tools", "", "--", prompt]
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
            log("running report.py with data captured before interrupt")
            run_report(script_dir, run_dir)
            print_comparison(run_dir, data_dir, window)
            log(f"resume with: {resume_command_for_run(window, run_dir)}")
            return

        try:
            snap = snapshot_metrics(run_dir, metrics_url, f"{n}-after", model)
        except Exception as exc:
            log(f"metrics endpoint unreachable after iteration {iter_num} ({exc}); proxy likely shut down — running report on data captured so far")
            run_report(script_dir, run_dir)
            print_comparison(run_dir, data_dir, window)
            log(f"resume with: {resume_command_for_run(window, run_dir)}")
            return
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

        used_units_since_tick += iter_units
        observed_units += iter_units
        if crossed > 0:
            observed_ticks += crossed
            est_units_per_tick = observed_units / observed_ticks
            used_units_since_tick = max(0.0, used_units_since_tick - est_units_per_tick * crossed)

        if interrupted:
            log("running report.py")
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
        util_pct = int(snap[util_key] * 100 + 1e-9)
        other_util_pct = int(snap[f"util_{other_window}"] * 100 + 1e-9)
        next_until_tick = max(0.0, est_units_per_tick - used_units_since_tick)

        crossed_str = green(str(crossed)) if crossed > 0 else dim(str(crossed))
        total_str = bold(str(ticks_now)) if ticks_now >= need else str(ticks_now)

        log(f"iteration {bold(iter_num)}: prompt size {cyan(size_name)}, took {wall_ms/1000:.1f} seconds")
        log_raw(
            f"  {window} window: utilization {yellow(f'{util_pct}%')}, "
            f"ticks crossed this call: {crossed_str}, "
            f"ticks crossed total: {total_str} of {need} needed, "
            f"input-equivalent tokens until next tick: ~{bold(f'{next_until_tick:,.0f}')}"
        )
        log_raw(
            f"  {other_window} window utilization (informational): {yellow(f'{other_util_pct}%')}"
        )
        cache_read_str = f"{usage['cache_read']:,}"
        cache_write_str = f"{usage['cache_create']:,}"
        log_raw(
            f"  tokens used this call: "
            f"input {magenta(usage['input'])}, "
            f"output {magenta(usage['output'])}, "
            f"cache read {magenta(cache_read_str)}, "
            f"cache write {magenta(cache_write_str)} "
            f"{dim(f'(input-equivalent total: {iter_units:,.0f})')}"
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

    if interrupted:
        log("running report.py")
        run_report(script_dir, run_dir)
        log(f"resume with: {resume_command_for_run(window, run_dir)}")
        return

    log("running report.py")
    run_report(script_dir, run_dir)
    print_comparison(run_dir, data_dir, window)

    log(f"run complete: {run_dir}")
    print(run_dir)


if __name__ == "__main__":
    main()
