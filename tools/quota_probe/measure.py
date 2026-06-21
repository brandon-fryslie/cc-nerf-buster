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

from tools.quota_probe.estimator import Estimate, estimate_usage_log


DEFAULT_MODEL = "claude-opus-4-7"
DEFAULT_CLAUDE_TIMEOUT_SECONDS = 180
# A probe that pushes utilization toward the limit is the workload most likely to
# be throttled, so transient failures are expected. Retry a call a few times,
# then skip the iteration; only give up entirely after many iterations in a row
# fail (the run genuinely cannot make progress).
MAX_CALL_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0
MAX_CONSECUTIVE_FAILURES = 5
DEFAULT_TICK_USD = {
    "5h": 2.75,
    "7d": 14.0,
}
DEFAULT_USD_PER_CHAR = 0.000003
MIN_PROMPT_CHARS = 200
MAX_PROMPT_CHARS = 250_000
PROMPT_PARAGRAPH = (
    "Operational measurement note. The request describes ordinary service "
    "activity, quota accounting, request logging, and stable capacity "
    "measurement. Reply with one short sentence confirming receipt."
)


@dataclass(frozen=True)
class DriveConfig:
    window: str
    run_dir: Path
    model: str
    target_relative_width: float
    max_iters: int
    dry_run: bool
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


def build_prompt(target_chars: int) -> str:
    target = max(MIN_PROMPT_CHARS, min(MAX_PROMPT_CHARS, target_chars))
    header = "Read the operational note and reply with exactly: ok\n\n"
    parts = [header]
    while len("".join(parts)) < target:
        parts.append(PROMPT_PARAGRAPH)
        parts.append("\n\n")
    return "".join(parts)[:target] + "\n"


def prompt_chars_for_estimate(estimate: Estimate, usd_per_char: float) -> int:
    tick = DEFAULT_TICK_USD[estimate.window]
    target_usd = tick * 0.10
    if estimate.interval is not None:
        target_usd = max(0.02, min(estimate.interval.mid * 0.20, max(estimate.interval.width / 2.0, estimate.interval.mid * 0.02)))
    return int(target_usd / max(usd_per_char, 1e-9))


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


def synthetic_usage_tokens(cost_usd: float) -> int:
    return max(1, int(round(cost_usd * 1_000_000.0 / 10.0)))


def synthetic_event(*, cost_usd: float, util_bucket: int, model: str, request_id: str) -> dict[str, Any]:
    tokens = synthetic_usage_tokens(cost_usd)
    return {
        "ts": utc_now(),
        "upstream": "api.anthropic.com",
        "model": model,
        "status": 200,
        "duration_ms": 1,
        "streaming": False,
        "errors": [],
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": tokens,
            "cache_creation_1h_input_tokens": tokens,
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


def append_synthetic_event(run_dir: Path, *, cfg: DriveConfig, actual_spend: float, cost_usd: float, iter_num: int) -> float:
    tick_usd = DEFAULT_TICK_USD[cfg.window]
    new_spend = actual_spend + cost_usd
    util_bucket = min(100, int(new_spend / tick_usd))
    append_jsonl(
        run_dir / "usage.jsonl",
        synthetic_event(
            cost_usd=cost_usd,
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
    if data["weighted_usd_per_tick"] is None:
        lines.append("No capacity estimate is available from this artifact set.")
    else:
        usd = data["weighted_usd_per_tick"]
        ocw = data["opus_cache_write_tokens"]["per_tick"]
        full = data["opus_cache_write_tokens"]["full_quota"]
        lines.extend([
            "## Estimate",
            "",
            f"- Weighted USD / 1% tick: low={usd['low']:.6f} mid={usd['midpoint']:.6f} high={usd['high']:.6f}",
            f"- Relative interval width: {usd['relative_width'] * 100:.3f}%",
            f"- Opus cache-write tokens / 1% tick: low={ocw['low']:.0f} mid={ocw['midpoint']:.0f} high={ocw['high']:.0f}",
            f"- Opus cache-write full quota: low={full['low']:.0f} mid={full['midpoint']:.0f} high={full['high']:.0f}",
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


def drive(cfg: DriveConfig) -> Estimate:
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    (cfg.run_dir / "prompts").mkdir(exist_ok=True)
    (cfg.run_dir / "outputs").mkdir(exist_ok=True)
    write_manifest(cfg.run_dir, cfg)

    usage_path = cfg.run_dir / "usage.jsonl"
    usd_per_char = DEFAULT_USD_PER_CHAR
    dry_spend = DEFAULT_TICK_USD[cfg.window] * 23.37

    if cfg.dry_run:
        dry_spend = append_synthetic_event(
            cfg.run_dir,
            cfg=cfg,
            actual_spend=dry_spend,
            cost_usd=0.001,
            iter_num=0,
        )
    else:
        if not attempt_claude_call("Reply with exactly: ok", cfg=cfg, iter_num=0, usage_path=usage_path):
            die("initial claude call failed after retries; cannot start measurement")

    estimate = estimate_run(cfg.run_dir, cfg.window)
    if not cfg.dry_run:
        reject_unusable_active_events(estimate)
    consecutive_failures = 0
    for iter_num in range(1, cfg.max_iters + 1):
        prompt_chars = prompt_chars_for_estimate(estimate, usd_per_char)
        prompt = build_prompt(prompt_chars)
        if cfg.dry_run:
            target_cost = max(0.02, min(DEFAULT_TICK_USD[cfg.window] * 0.35, prompt_chars * usd_per_char))
            (cfg.run_dir / "prompts" / f"{iter_num:03d}.txt").write_text(prompt)
            (cfg.run_dir / "outputs" / f"{iter_num:03d}.txt").write_text("ok\n")
            dry_spend = append_synthetic_event(
                cfg.run_dir,
                cfg=cfg,
                actual_spend=dry_spend,
                cost_usd=target_cost,
                iter_num=iter_num,
            )
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
        estimate = estimate_run(cfg.run_dir, cfg.window)
        if not cfg.dry_run:
            reject_unusable_active_events(estimate)
        if estimate.interval is not None:
            usd_per_char = max(1e-9, estimate.measured_cost_usd / max(1, sum_prompt_chars(cfg.run_dir)))
            if estimate.interval.relative_width <= cfg.target_relative_width:
                return estimate
    return estimate


def sum_prompt_chars(run_dir: Path) -> int:
    total = 0
    for path in (run_dir / "prompts").glob("*.txt"):
        total += len(path.read_text())
    return total


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
        cfg = DriveConfig(
            window=args.window,
            run_dir=run_dir,
            model=args.model,
            target_relative_width=args.target_relative_width,
            max_iters=args.max_iters,
            dry_run=args.dry_run,
            claude_timeout_seconds=args.claude_timeout_seconds,
        )
        estimate = drive(cfg)
        print(render_report(estimate.to_json()))
        if estimate.status != "estimated":
            raise SystemExit(2)


if __name__ == "__main__":
    main()
