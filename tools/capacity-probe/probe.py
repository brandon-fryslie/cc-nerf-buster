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

PROMPT_CHAR_TARGETS = {
    "large": 100_000,
    "medium": 36_000,
    "small": 10_000,
    "micro": 3_000,
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


PROMPTS = {
    name: build_prompt_corpus(target_chars)
    for name, target_chars in PROMPT_CHAR_TARGETS.items()
}

PROMPT_STATS = {
    name: {"chars": len(text), "words": len(text.split())}
    for name, text in PROMPTS.items()
}

DEFAULT_5H_INPUT_EQUIV_PER_TICK = 74543.815
DEFAULT_7D_INPUT_EQUIV_PER_TICK = 400000.0

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


def log(msg: str) -> None:
    print(f"[probe {datetime.now(UTC).strftime('%H:%M:%S')}] {msg}", file=sys.stderr)


def die(msg: str) -> "NoReturn":
    log(f"ERROR: {msg}")
    raise SystemExit(1)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--window", choices=("5h", "7d", "both"), default="both")
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
    die(f"metric {name} not found")


def parse_model_counter_total(metrics_text: str, metric_name: str, model: str) -> int:
    prefix = f'{metric_name}{{model="{model}",'
    total = 0
    for line in metrics_text.splitlines():
        if line.startswith(prefix):
            total += int(float(line.rsplit(" ", 1)[-1]))
    return total


def snapshot_metrics(run_dir: Path, metrics_url: str, label: str, model: str) -> dict:
    raw_text = fetch_metrics(metrics_url)
    raw_path = run_dir / "raw-metrics" / f"{label}.prom"
    raw_path.write_text(raw_text)

    snap = {
        "label": label,
        "ts": utc_now(),
        "util_5h": parse_gauge(raw_text, "ccnb_quota_5h_utilization"),
        "util_7d": parse_gauge(raw_text, "ccnb_quota_7d_utilization"),
        "cost_total": parse_gauge(raw_text, "ccnb_cost_total"),
        "capacity_usd_5h": parse_gauge(raw_text, "ccnb_quota_5h_estimated_capacity_usd"),
        "capacity_usd_7d": parse_gauge(raw_text, "ccnb_quota_7d_estimated_capacity_usd"),
        "no_model_input_tokens": parse_gauge(raw_text, "ccnb_no_model_error_input_tokens_total"),
        "no_model_output_tokens": parse_gauge(raw_text, "ccnb_no_model_error_output_tokens_total"),
        "model_requests": parse_model_counter_total(raw_text, "ccnb_requests_total", model),
        "model_input_tokens": parse_model_counter_total(raw_text, "ccnb_input_tokens_total", model),
        "model_output_tokens": parse_model_counter_total(raw_text, "ccnb_output_tokens_total", model),
        "model_cache_creation_input_tokens": parse_model_counter_total(raw_text, "ccnb_cache_creation_input_tokens_total", model),
        "model_cache_read_input_tokens": parse_model_counter_total(raw_text, "ccnb_cache_read_input_tokens_total", model),
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
    window: str,
    ticks_5h: int,
    ticks_7d: int,
    need_5h: int,
    need_7d: int,
    est_units_5h: float,
    est_units_7d: float,
    used_units_5h: float,
    used_units_7d: float,
) -> tuple[str, str, float]:
    candidates: list[tuple[str, float]] = []
    if window in ("5h", "both") and ticks_5h < need_5h and est_units_5h > 0:
        candidates.append(("5h", max(0.0, est_units_5h - used_units_5h)))
    if window in ("7d", "both") and ticks_7d < need_7d and est_units_7d > 0:
        candidates.append(("7d", max(0.0, est_units_7d - used_units_7d)))
    if not candidates:
        return ("5h" if window != "7d" else "7d", "large", 0.0)

    control_window, remaining_units = min(candidates, key=lambda item: item[1])
    est_units = est_units_5h if control_window == "5h" else est_units_7d
    remaining_ratio = remaining_units / est_units if est_units > 0 else 1.0
    # // [LAW:dataflow-not-control-flow] every iteration follows the same
    # choose-build-send-measure sequence; only the prompt size value changes.
    if remaining_ratio > 0.50:
        size = "large"
    elif remaining_ratio > 0.20:
        size = "medium"
    elif remaining_ratio > 0.08:
        size = "small"
    else:
        size = "micro"
    return control_window, size, remaining_units


def target_met(window: str, ticks_5h: int, ticks_7d: int, need_5h: int, need_7d: int) -> bool:
    met_5h = ticks_5h >= need_5h
    met_7d = ticks_7d >= need_7d
    if window == "5h":
        return met_5h
    if window == "7d":
        return met_7d
    return met_5h and met_7d


def build_prompt(size_name: str) -> str:
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


def resume_command(window: str) -> str:
    return resume_command_for_run(window, None)


def resume_command_for_run(window: str, run_dir: Path | None) -> str:
    recipe = {
        "both": "probe",
        "5h": "probe-5h",
        "7d": "probe-7d",
    }[window]
    if run_dir is None:
        return f"just {recipe}"
    return f"just {recipe} --resume {run_dir}"


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
    proxy_url = "http://localhost:9480"
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


def load_resume_state(run_dir: Path) -> tuple[dict, dict, int, float, float]:
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        die(f"resume run missing manifest: {manifest_path}")

    manifest = json.loads(manifest_path.read_text())
    baseline = manifest["baseline"]
    est_units_per_tick_5h = float(manifest["estimated_input_equiv_tokens_per_tick_5h"])
    est_units_per_tick_7d = float(manifest["estimated_input_equiv_tokens_per_tick_7d"])

    snapshots = {row["label"]: row for row in load_jsonl(run_dir / "snapshots.jsonl")}
    iterations = load_jsonl(run_dir / "iterations.jsonl")

    current = baseline
    used_units_since_5h_tick = 0.0
    used_units_since_7d_tick = 0.0

    for row in iterations:
        label = f"{int(row['iter']):03d}-after"
        snap = snapshots.get(label)
        if snap is None:
            break

        iter_units = float(row["input_equivalent_tokens"])
        prev_ticks_5h = tick_delta(current["util_5h"], baseline["util_5h"])
        prev_ticks_7d = tick_delta(current["util_7d"], baseline["util_7d"])
        new_ticks_5h = tick_delta(snap["util_5h"], baseline["util_5h"])
        new_ticks_7d = tick_delta(snap["util_7d"], baseline["util_7d"])
        crossed_5h = max(0, new_ticks_5h - prev_ticks_5h)
        crossed_7d = max(0, new_ticks_7d - prev_ticks_7d)

        used_units_since_5h_tick += iter_units
        used_units_since_7d_tick += iter_units
        if crossed_5h > 0 and est_units_per_tick_5h > 0:
            used_units_since_5h_tick = max(0.0, used_units_since_5h_tick - est_units_per_tick_5h * crossed_5h)
        if crossed_7d > 0 and est_units_per_tick_7d > 0:
            used_units_since_7d_tick = max(0.0, used_units_since_7d_tick - est_units_per_tick_7d * crossed_7d)

        current = snap

    next_iter = 1
    if iterations:
        next_iter = int(iterations[-1]["iter"]) + 1

    return current, baseline, next_iter, used_units_since_5h_tick, used_units_since_7d_tick


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

    metrics_url = os.environ.get("METRICS_URL", "http://localhost:9481/metrics")
    data_dir = Path(os.environ.get("DATA_DIR", str(Path.home() / ".local" / "cc-nerf-buster")))
    model = os.environ.get("MODEL", "claude-opus-4-7")
    target_5h_ticks = int(os.environ.get("TARGET_5H_TICKS", "3"))
    target_7d_ticks = int(os.environ.get("TARGET_7D_TICKS", "1"))
    dry_iterations = int(os.environ.get("DRY_ITERATIONS", "3"))

    if args.continue_latest:
        args.resume = str(resolve_continue_run(data_dir, args.window, args.dry_run))

    script_dir = Path(__file__).resolve().parent
    ts = run_ts()
    run_dir = Path(args.resume).expanduser() if args.resume else data_dir / "probe-runs" / (f"dryrun-{ts}" if args.dry_run else ts)
    (run_dir / "raw-metrics").mkdir(parents=True, exist_ok=True)
    (run_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (run_dir / "claude-output").mkdir(parents=True, exist_ok=True)

    if args.window == "5h":
        log(f"window: 5h only (target: {target_5h_ticks} tick(s))")
    elif args.window == "7d":
        log(f"window: 7d only (target: {target_7d_ticks} tick(s))")
    else:
        log(f"window: both (target: 5h={target_5h_ticks}, 7d={target_7d_ticks})")
    if args.dry_run:
        log(f"DRY RUN: 'echo' replaces 'claude'; stopping after {dry_iterations} iteration(s). No API calls will be made.")

    try:
        fetch_metrics(metrics_url)
    except Exception as exc:
        die(f"metrics endpoint {metrics_url} not reachable: {exc}")
    if not args.dry_run and not (data_dir / "ca.crt").exists():
        die(f"missing CA cert at {data_dir / 'ca.crt'}; run 'just install' first")

    copy_with_hashes(run_dir, script_dir)

    if args.resume:
        manifest = json.loads((run_dir / "manifest.json").read_text())
        baseline = manifest["baseline"]
        baseline_zero_5h = bool(manifest["baseline_zero_5h"])
        baseline_zero_7d = bool(manifest["baseline_zero_7d"])
        need_5h = int(manifest["required_crossings_5h"])
        need_7d = int(manifest["required_crossings_7d"])
        est_units_per_tick_5h = float(manifest["estimated_input_equiv_tokens_per_tick_5h"])
        est_units_per_tick_7d = float(manifest["estimated_input_equiv_tokens_per_tick_7d"])
        current, baseline, next_iter, used_units_since_5h_tick, used_units_since_7d_tick = load_resume_state(run_dir)
        log(
            f"resuming run: {run_dir} "
            f"(next_iter={next_iter:03d}, used_5h={used_units_since_5h_tick:.1f}, used_7d={used_units_since_7d_tick:.1f})"
        )
    else:
        log("fetching baseline snapshot")
        baseline = snapshot_metrics(run_dir, metrics_url, "000-baseline", model)
        baseline_zero_5h = is_zero_util(baseline["util_5h"])
        baseline_zero_7d = is_zero_util(baseline["util_7d"])
        need_5h = target_5h_ticks + 1 - int(baseline_zero_5h)
        need_7d = target_7d_ticks + 1 - int(baseline_zero_7d)
        input_price = input_price_per_mtok(model)
        est_units_per_tick_5h = baseline["capacity_usd_5h"] * 0.01 * 1_000_000.0 / input_price
        est_units_per_tick_7d = baseline["capacity_usd_7d"] * 0.01 * 1_000_000.0 / input_price
        if est_units_per_tick_5h <= 0:
            est_units_per_tick_5h = float(os.environ.get("ESTIMATE_5H_INPUT_EQUIV_PER_TICK", str(DEFAULT_5H_INPUT_EQUIV_PER_TICK)))
        if est_units_per_tick_7d <= 0:
            est_units_per_tick_7d = float(os.environ.get("ESTIMATE_7D_INPUT_EQUIV_PER_TICK", str(DEFAULT_7D_INPUT_EQUIV_PER_TICK)))

        manifest = {
            "started": ts,
            "metrics_url": metrics_url,
            "model": model,
            "window": args.window,
            "target_5h_ticks": target_5h_ticks,
            "target_7d_ticks": target_7d_ticks,
            "required_crossings_5h": need_5h,
            "required_crossings_7d": need_7d,
            "baseline_zero_5h": baseline_zero_5h,
            "baseline_zero_7d": baseline_zero_7d,
            "dry_run": args.dry_run,
            "dry_iterations": dry_iterations,
            "baseline": baseline,
            "prompt_stats": PROMPT_STATS,
            "system_prompt": "",
            "estimated_input_equiv_tokens_per_tick_5h": est_units_per_tick_5h,
            "estimated_input_equiv_tokens_per_tick_7d": est_units_per_tick_7d,
        }
        (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

        current = baseline
        used_units_since_5h_tick = 0.0
        used_units_since_7d_tick = 0.0
        next_iter = 1

    interrupted = False

    def handle_interrupt(_signum: int, _frame: object) -> None:
        nonlocal interrupted
        interrupted = True
        log("interrupt received; will stop after current iteration")

    signal.signal(signal.SIGINT, handle_interrupt)
    signal.signal(signal.SIGTERM, handle_interrupt)

    ticks_5h = tick_delta(current["util_5h"], baseline["util_5h"])
    ticks_7d = tick_delta(current["util_7d"], baseline["util_7d"])
    if target_met(args.window, ticks_5h, ticks_7d, need_5h, need_7d):
        log(
            f"resume target already reached: 5h {ticks_5h}/{need_5h}, "
            f"7d {ticks_7d}/{need_7d}"
        )
        log("running report.py")
        subprocess.run([sys.executable, str(script_dir / "report.py"), str(run_dir)], check=True)
        log(f"run complete: {run_dir}")
        print(run_dir)
        return

    for iter_num in range(next_iter, 10_000):
        if interrupted:
            break

        n = f"{iter_num:03d}"
        ticks_5h = tick_delta(current["util_5h"], baseline["util_5h"])
        ticks_7d = tick_delta(current["util_7d"], baseline["util_7d"])
        control_window, size_name, rem = choose_prompt_size(
            args.window,
            ticks_5h,
            ticks_7d,
            need_5h,
            need_7d,
            est_units_per_tick_5h,
            est_units_per_tick_7d,
            used_units_since_5h_tick,
            used_units_since_7d_tick,
        )

        prompt = f"Probe timestamp: {utc_now()}\n" + build_prompt(size_name)
        prompt_path = run_dir / "prompts" / f"{n}.txt"
        prompt_path.write_text(prompt)
        output_path = run_dir / "claude-output" / f"{n}.txt"

        cmd_name = "python3" if args.dry_run else "claude"
        cmd = [cmd_name]
        cmd_desc = cmd_name
        if not args.dry_run:
            # [LAW:single-enforcer] `--` is the single boundary that stops the
            # variadic `--tools` option from consuming the positional prompt.
            cmd = [cmd_name, "-p", "--model", model, "--system-prompt", "", "--no-session-persistence", "--tools", "", "--", prompt]
            cmd_desc = f"{cmd_name} -p --model {model} --system-prompt '' --"
        else:
            cmd = [cmd_name, "-c", "import sys; print(sys.argv[1])", prompt]
        log(f"iter {n}: {cmd_desc} ({size_name}, remaining_tick={rem:.3f} on {control_window})")

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

        snap = snapshot_metrics(run_dir, metrics_url, f"{n}-after", model)
        usage = {
            "requests": snap["model_requests"] - current["model_requests"],
            "input": snap["model_input_tokens"] - current["model_input_tokens"],
            "output": snap["model_output_tokens"] - current["model_output_tokens"],
            "cache_create": snap["model_cache_creation_input_tokens"] - current["model_cache_creation_input_tokens"],
            "cache_read": snap["model_cache_read_input_tokens"] - current["model_cache_read_input_tokens"],
        }
        priced_iter_units = quota_input_equivalent_tokens(usage)
        iter_units = priced_iter_units
        prev_ticks_5h = tick_delta(current["util_5h"], baseline["util_5h"])
        prev_ticks_7d = tick_delta(current["util_7d"], baseline["util_7d"])
        new_ticks_5h = tick_delta(snap["util_5h"], baseline["util_5h"])
        new_ticks_7d = tick_delta(snap["util_7d"], baseline["util_7d"])
        crossed_5h = max(0, new_ticks_5h - prev_ticks_5h)
        crossed_7d = max(0, new_ticks_7d - prev_ticks_7d)

        used_units_since_5h_tick += iter_units
        used_units_since_7d_tick += iter_units
        if crossed_5h > 0 and est_units_per_tick_5h > 0:
            used_units_since_5h_tick = max(0.0, used_units_since_5h_tick - est_units_per_tick_5h * crossed_5h)
        if crossed_7d > 0 and est_units_per_tick_7d > 0:
            used_units_since_7d_tick = max(0.0, used_units_since_7d_tick - est_units_per_tick_7d * crossed_7d)

        if interrupted:
            log(f"resume with: {resume_command_for_run(args.window, run_dir)}")
            return

        iteration = {
            "iter": iter_num,
            "ts": utc_now(),
            "prompt_file": str(prompt_path),
            "output_file": str(output_path),
            "prompt_size": size_name,
            "prompt_chars": PROMPT_STATS[size_name]["chars"],
            "prompt_words": PROMPT_STATS[size_name]["words"],
            "control_window": control_window,
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

        if completed.returncode != 0:
            log(f"iter {n}: claude exited {completed.returncode}; output={output_summary}")

        if usage["requests"] == 0:
            log(f"iter {n}: no API requests observed; claude output={output_summary}")
            fatal_reason = fatal_output_reason(completed.stdout)
            # // [LAW:single-enforcer] local CLI failures should be diagnosed at
            # the probe boundary once, instead of silently leaking as zero-usage iterations.
            if fatal_reason is not None:
                die(f"claude failed locally before any proxied request: {fatal_reason}")

        log(
            f"iter {n}: req={usage['requests']} "
            f"in={usage['input']} out={usage['output']} "
            f"cache_create={usage['cache_create']} cache_read={usage['cache_read']} "
            f"priced_input_equiv={priced_iter_units:.1f} "
            f"input_equiv={iter_units:.1f} "
            f"| util_5h={snap['util_5h']} (+{tick_delta(snap['util_5h'], baseline['util_5h'])}) "
            f"util_7d={snap['util_7d']} (+{tick_delta(snap['util_7d'], baseline['util_7d'])})"
        )

        current = snap
        ticks_5h = tick_delta(current["util_5h"], baseline["util_5h"])
        ticks_7d = tick_delta(current["util_7d"], baseline["util_7d"])

        if args.dry_run:
            if iter_num >= dry_iterations:
                log(f"DRY RUN: completed {iter_num} iteration(s); stopping (observed {ticks_5h}/{ticks_7d})")
                break
            continue

        met_5h = ticks_5h >= need_5h
        met_7d = ticks_7d >= need_7d
        if args.window == "5h" and met_5h:
            log(f"target reached (5h): observed {ticks_5h} crossings (need {need_5h}) → {target_5h_ticks} clean tick(s) across {iter_num} iterations")
            break
        if args.window == "7d" and met_7d:
            log(f"target reached (7d): observed {ticks_7d} crossings (need {need_7d}) → {target_7d_ticks} clean tick(s) across {iter_num} iterations")
            break
        if args.window == "both" and met_5h and met_7d:
            log(f"target reached (both): 5h observed {ticks_5h}/{need_5h} crossings / 7d observed {ticks_7d}/{need_7d} crossings across {iter_num} iterations")
            break

    if interrupted:
        log(f"resume with: {resume_command_for_run(args.window, run_dir)}")
        return

    log("running report.py")
    subprocess.run([sys.executable, str(script_dir / "report.py"), str(run_dir)], check=True)

    log(f"run complete: {run_dir}")
    print(run_dir)


if __name__ == "__main__":
    main()
