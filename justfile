default: check

build:
    go build -o cc-nerf-buster .

run *ARGS: build
    ./cc-nerf-buster {{ARGS}}

fmt:
    gofmt -w .

check: fmt vet build

vet:
    go vet ./...

test:
    go test ./...

clean:
    rm -f cc-nerf-buster

install *ARGS:
    ./scripts/install.sh {{ARGS}}

uninstall:
    ./scripts/uninstall.sh

# Note: each probe run targets exactly one window — a single prompt-size knob
# cannot satisfy both 5h and 7d tick boundaries at the same time.
#
# Capacity probe: 5h window.
probe-5h *ARGS:
    cd tools/capacity-probe && bash with-proxy.sh --window=5h {{ARGS}}

# Capacity probe: 7d window.
probe-7d *ARGS:
    cd tools/capacity-probe && bash with-proxy.sh --window=7d {{ARGS}}

# Capacity probe dry-run, 5h: exercises every code path with `echo` replacing `claude`.
probe-dry-5h *ARGS:
    cd tools/capacity-probe && bash with-proxy.sh --dry-run --window=5h {{ARGS}}

# Capacity probe dry-run, 7d: exercises every code path with `echo` replacing `claude`.
probe-dry-7d *ARGS:
    cd tools/capacity-probe && bash with-proxy.sh --dry-run --window=7d {{ARGS}}

# Regenerate report.md from an existing capacity-probe run directory.
probe-report RUN_DIR:
    python3 tools/capacity-probe/report.py {{RUN_DIR}}

# Recompute and print low/mid/high quota bounds from an existing probe run.
probe-bounds RUN_DIR:
    python3 tools/capacity-probe/report.py --print-bounds {{RUN_DIR}}
