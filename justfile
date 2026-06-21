default: check

build:
    go build -o cc-nerf-buster .

build-mock:
    go build -o mock-anthropic ./cmd/mock-anthropic

run *ARGS: build
    ./cc-nerf-buster {{ARGS}}

fmt:
    gofmt -w .

check: fmt vet build build-mock

vet:
    go vet ./...

test:
    go test ./...

quota-test:
    uv run --with pytest python -m pytest tools/quota_probe/test_estimator.py tools/quota_probe/test_measure.py -v

clean:
    rm -f cc-nerf-buster

install *ARGS:
    ./scripts/install.sh {{ARGS}}

uninstall:
    ./scripts/uninstall.sh

# Fresh event-sourced quota probe: 5h window.
quota-5h *ARGS:
    bash tools/quota_probe/with_proxy.sh --window=5h {{ARGS}}

# Fresh event-sourced quota probe: 7d window.
quota-7d *ARGS:
    bash tools/quota_probe/with_proxy.sh --window=7d {{ARGS}}

# Fresh quota probe dry-run, 5h: generates synthetic usage.jsonl and reports from it.
quota-dry-5h *ARGS:
    python3 tools/quota_probe/measure.py drive --dry-run --window=5h {{ARGS}}

# Fresh quota probe dry-run, 7d: generates synthetic usage.jsonl and reports from it.
quota-dry-7d *ARGS:
    python3 tools/quota_probe/measure.py drive --dry-run --window=7d {{ARGS}}

# Regenerate fresh quota report from an existing run directory.
quota-report RUN_DIR WINDOW:
    python3 tools/quota_probe/measure.py report {{RUN_DIR}} --window {{WINDOW}} --print

# Note: each probe run targets exactly one window — a single prompt-size knob
# cannot satisfy both 5h and 7d tick boundaries at the same time.
#
# Legacy capacity probe: 5h window.
probe-5h *ARGS:
    cd tools/capacity_probe && bash with-proxy.sh --window=5h {{ARGS}}

# Legacy capacity probe: 7d window.
probe-7d *ARGS:
    cd tools/capacity_probe && bash with-proxy.sh --window=7d {{ARGS}}

# Legacy capacity probe dry-run, 5h: exercises every code path with `echo` replacing `claude`.
probe-dry-5h *ARGS:
    cd tools/capacity_probe && bash with-proxy.sh --dry-run --window=5h {{ARGS}}

# Legacy capacity probe dry-run, 7d: exercises every code path with `echo` replacing `claude`.
probe-dry-7d *ARGS:
    cd tools/capacity_probe && bash with-proxy.sh --dry-run --window=7d {{ARGS}}

# Regenerate report.md from an existing capacity_probe run directory.
probe-report RUN_DIR:
    python3 tools/capacity_probe/report.py {{RUN_DIR}}

# Recompute and print low/mid/high quota bounds from an existing probe run.
probe-bounds RUN_DIR:
    python3 tools/capacity_probe/report.py --print-bounds {{RUN_DIR}}

# Compute quota capacity from all observed traffic in usage.jsonl (no quota spent).
passive-report *ARGS:
    python3 tools/capacity_probe/passive-report.py {{ARGS}}
