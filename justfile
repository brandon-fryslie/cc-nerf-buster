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

probe-test:
    uv run --with pytest python -m pytest tools/quota_probe/test_estimator.py tools/quota_probe/test_measure.py -v

clean:
    rm -f cc-nerf-buster

install *ARGS:
    ./scripts/install.sh {{ARGS}}

uninstall:
    ./scripts/uninstall.sh

# Each probe run targets exactly one window — a single prompt-size knob cannot
# satisfy both 5h and 7d tick boundaries at once. Set TARGET_5H_TICKS /
# TARGET_7D_TICKS to stop after that many observed 1% crossings; otherwise the
# run stops once the estimate interval is tight enough.

# Measure the 5h quota window against live Anthropic traffic.
probe-5h *ARGS:
    bash tools/quota_probe/with_proxy.sh --window=5h {{ARGS}}

# Measure the 7d quota window against live Anthropic traffic.
probe-7d *ARGS:
    bash tools/quota_probe/with_proxy.sh --window=7d {{ARGS}}

# Dry-run the 5h probe: synthetic usage.jsonl, no real claude calls or quota spend.
probe-dry-5h *ARGS:
    python3 tools/quota_probe/measure.py drive --dry-run --window=5h {{ARGS}}

# Dry-run the 7d probe: synthetic usage.jsonl, no real claude calls or quota spend.
probe-dry-7d *ARGS:
    python3 tools/quota_probe/measure.py drive --dry-run --window=7d {{ARGS}}

# Re-estimate and print the quota report from an existing run directory.
probe-report RUN_DIR WINDOW:
    python3 tools/quota_probe/measure.py report {{RUN_DIR}} --window {{WINDOW}} --print

# Compute quota capacity from all observed traffic in usage.jsonl (no quota spent).
passive-report *ARGS:
    python3 tools/capacity_probe/passive-report.py {{ARGS}}
