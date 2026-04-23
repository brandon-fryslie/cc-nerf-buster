#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen


PROMPTS = {
    "large": """Return exactly the single lowercase letter x.
Do not add any other text.
Read the project notes below and then answer with exactly x.

Project notes:
The service team reviewed several days of routine traffic and found that request mix, queue depth, and model selection all remained within the same broad operating range. Engineers recorded ordinary maintenance work, a few predictable bursts around the top of the hour, and no unusual fallback behavior. Internal summaries noted that most requests completed normally, most follow-up work was administrative, and the system showed the same kind of regular usage pattern that appears in uneventful weeks.

The review also described ongoing housekeeping tasks. Operators compared reports from the proxy, checked that quota headers still arrived on successful responses, and verified that the logging pipeline continued to write structured records without format drift. They highlighted stable request pacing, recurring batch jobs, and the usual mix of shorter interactive calls and longer background jobs. No one described a surprising outage, an emergency fix, or a sudden change in policy. The overall impression was of a normal service under familiar load.

Another section summarized customer behavior in plain language. Most users sent straightforward prompts, waited for a reply, and moved on to the next task without unusual retry patterns. A few teams performed large document reviews, while others focused on short coding questions or routine operational checks. Analysts remarked that the variety of work was broad but not chaotic, and that the token footprint looked consistent with previous periods in which capacity estimates stayed stable enough to support planning decisions.

The final notes covered process rather than incidents. Team members emphasized that instrumentation should stay simple, measurements should remain auditable, and any calibration workflow should favor predictable outputs over clever prompting tricks. They argued that a useful probe is one that produces ordinary language on the way in, a small and controlled answer on the way out, and a clear measurement trail that can be checked later. The memo ended by saying that steady, plain, reproducible traffic is more valuable than complicated experiments that are hard to reason about after the fact.
""",
    "medium": """Return exactly the single lowercase letter x.
Do not add any other text.
Read the notes below and then answer with exactly x.

Project notes:
The operations summary described a normal stretch of service activity with no unusual incident response, no emergency maintenance, and no surprising traffic spikes outside the patterns the team already expects. Most requests were ordinary interactive prompts, followed by smaller groups of scheduled background work and routine verification jobs.

Engineers checked that quota headers, usage extraction, and JSONL logging all continued to behave normally. They wrote that the system looked stable, the request mix stayed familiar, and the measurement path remained useful because it favored plain prompts, simple outputs, and easy-to-audit records instead of complicated probing tricks.
""",
    "small": """Return exactly the single lowercase letter x.
Do not add any other text.
Read the notes below and then answer with exactly x.

Project notes:
The team reported routine traffic, stable request handling, normal background jobs, and no unusual operational changes. They also noted that measurements should rely on plain prompts, predictable outputs, and simple audit trails.
""",
    "micro": """Return exactly the single lowercase letter x.
Do not add any other text.
Read the note below and then answer with exactly x.

Project note:
The service looked normal and the measurement workflow should stay simple and predictable.
""",
}

PROMPT_STATS = {
    name: {"chars": len(text), "words": len(text.split())}
    for name, text in PROMPTS.items()
}


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
    return p.parse_args()


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


def snapshot_metrics(run_dir: Path, metrics_url: str, label: str) -> dict:
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
    }
    with (run_dir / "snapshots.jsonl").open("a") as f:
        f.write(json.dumps(snap) + "\n")
    return snap


def tick_delta(current_util: float, baseline_util: float) -> int:
    return int(current_util * 100 + 1e-9) - int(baseline_util * 100 + 1e-9)


def is_zero_util(util: float) -> bool:
    return abs(util) <= 1e-9


def remaining_ticks(util: float) -> float:
    scaled = util * 100.0
    bucket = math.floor(scaled + 1e-9)
    rem = (bucket + 1) - scaled
    return 1.0 if rem <= 1e-9 else rem


def choose_prompt_size(
    window: str,
    util_5h: float,
    util_7d: float,
    ticks_5h: int,
    ticks_7d: int,
    need_5h: int,
    need_7d: int,
) -> tuple[str, str, float]:
    candidates: list[tuple[str, float]] = []
    if window in ("5h", "both") and ticks_5h < need_5h:
        candidates.append(("5h", remaining_ticks(util_5h)))
    if window in ("7d", "both") and ticks_7d < need_7d:
        candidates.append(("7d", remaining_ticks(util_7d)))
    if not candidates:
        candidates.append(("5h" if window != "7d" else "7d", 1.0))

    control_window, rem = min(candidates, key=lambda item: item[1])
    # // [LAW:dataflow-not-control-flow] every iteration follows the same
    # choose-build-send-measure sequence; only the prompt size value changes.
    if rem > 0.50:
        size = "large"
    elif rem > 0.20:
        size = "medium"
    elif rem > 0.08:
        size = "small"
    else:
        size = "micro"
    return control_window, size, rem


def build_prompt(size_name: str) -> str:
    return PROMPTS[size_name]


def line_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open() as f:
        return sum(1 for _ in f)


def slice_usage(path: Path, start_line: int) -> dict:
    if not path.exists():
        return {
            "requests": 0,
            "input": 0,
            "output": 0,
            "cache_create": 0,
            "cache_read": 0,
            "models": [],
        }

    requests = 0
    input_tokens = 0
    output_tokens = 0
    cache_create = 0
    cache_read = 0
    models: set[str] = set()

    with path.open() as f:
        for idx, line in enumerate(f, start=1):
            if idx <= start_line or not line.strip():
                continue
            row = json.loads(line)
            usage = row.get("usage") or {}
            requests += 1
            input_tokens += int(usage.get("input_tokens") or 0)
            output_tokens += int(usage.get("output_tokens") or 0)
            cache_create += int(usage.get("cache_creation_input_tokens") or 0)
            cache_read += int(usage.get("cache_read_input_tokens") or 0)
            if row.get("model"):
                models.add(row["model"])

    return {
        "requests": requests,
        "input": input_tokens,
        "output": output_tokens,
        "cache_create": cache_create,
        "cache_read": cache_read,
        "models": sorted(models),
    }


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


def main() -> None:
    args = parse_args()

    metrics_url = os.environ.get("METRICS_URL", "http://localhost:9481/metrics")
    data_dir = Path(os.environ.get("DATA_DIR", str(Path.home() / ".local" / "cc-nerf-buster")))
    usage_log = Path(os.environ.get("USAGE_LOG", str(data_dir / "usage.jsonl")))
    model = os.environ.get("MODEL", "claude-opus-4-7")
    target_5h_ticks = int(os.environ.get("TARGET_5H_TICKS", "3"))
    target_7d_ticks = int(os.environ.get("TARGET_7D_TICKS", "1"))
    dry_iterations = int(os.environ.get("DRY_ITERATIONS", "3"))

    script_dir = Path(__file__).resolve().parent
    ts = run_ts()
    run_dir = data_dir / "probe-runs" / (f"dryrun-{ts}" if args.dry_run else ts)
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

    copy_with_hashes(run_dir, script_dir)

    log("fetching baseline snapshot")
    baseline = snapshot_metrics(run_dir, metrics_url, "000-baseline")
    baseline_zero_5h = is_zero_util(baseline["util_5h"])
    baseline_zero_7d = is_zero_util(baseline["util_7d"])
    need_5h = target_5h_ticks + 1 - int(baseline_zero_5h)
    need_7d = target_7d_ticks + 1 - int(baseline_zero_7d)

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
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    current = baseline
    baseline_no_in = baseline["no_model_input_tokens"]
    baseline_no_out = baseline["no_model_output_tokens"]

    for iter_num in range(1, 10_000):
        n = f"{iter_num:03d}"
        ticks_5h = tick_delta(current["util_5h"], baseline["util_5h"])
        ticks_7d = tick_delta(current["util_7d"], baseline["util_7d"])
        control_window, size_name, rem = choose_prompt_size(
            args.window,
            current["util_5h"],
            current["util_7d"],
            ticks_5h,
            ticks_7d,
            need_5h,
            need_7d,
        )

        prompt = build_prompt(size_name)
        prompt_path = run_dir / "prompts" / f"{n}.txt"
        prompt_path.write_text(prompt)
        output_path = run_dir / "claude-output" / f"{n}.txt"

        cmd_name = "cat" if args.dry_run else "claude"
        cmd = [cmd_name]
        cmd_desc = cmd_name
        if not args.dry_run:
            cmd = [cmd_name, "-p", "--model", model, "--bare", "--system-prompt", "", "--no-session-persistence", "--tools", ""]
            cmd_desc = f"{cmd_name} -p --model {model} --bare --system-prompt ''"
        log(f"iter {n}: {cmd_desc} ({size_name}, remaining_tick={rem:.3f} on {control_window})")

        usage_before = line_count(usage_log)
        start = time.time_ns()
        completed = subprocess.run(cmd, input=prompt, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        wall_ms = (time.time_ns() - start) // 1_000_000
        output_path.write_text(completed.stdout)

        snap = snapshot_metrics(run_dir, metrics_url, f"{n}-after")
        if snap["no_model_input_tokens"] != baseline_no_in or snap["no_model_output_tokens"] != baseline_no_out:
            die(
                "unpriced-model tokens advanced "
                f"(input: {baseline_no_in} → {snap['no_model_input_tokens']}, "
                f"output: {baseline_no_out} → {snap['no_model_output_tokens']})."
            )

        usage = slice_usage(usage_log, usage_before)
        iteration = {
            "iter": iter_num,
            "ts": utc_now(),
            "prompt_file": str(prompt_path),
            "output_file": str(output_path),
            "prompt_size": size_name,
            "prompt_chars": PROMPT_STATS[size_name]["chars"],
            "prompt_words": PROMPT_STATS[size_name]["words"],
            "control_window": control_window,
            "remaining_ticks_before_call": rem,
            "exit_code": completed.returncode,
            "wall_ms": wall_ms,
            "requests": usage["requests"],
            "input_tokens": usage["input"],
            "output_tokens": usage["output"],
            "cache_creation_input_tokens": usage["cache_create"],
            "cache_read_input_tokens": usage["cache_read"],
            "models": usage["models"],
        }
        with (run_dir / "iterations.jsonl").open("a") as f:
            f.write(json.dumps(iteration) + "\n")

        log(
            f"iter {n}: req={usage['requests']} in={usage['input']} out={usage['output']} "
            f"cache_create={usage['cache_create']} cache_read={usage['cache_read']} "
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

    log("running report.py")
    subprocess.run([sys.executable, str(script_dir / "report.py"), str(run_dir)], check=True)

    log(f"run complete: {run_dir}")
    print(run_dir)


if __name__ == "__main__":
    main()
