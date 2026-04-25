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

# Capacity probe: measure both 5h and 7d (stops when both hit their tick targets).
probe *ARGS:
    cd tools/capacity-probe && bash probe.sh --window=both {{ARGS}}

# Capacity probe: 5h only.
probe-5h *ARGS:
    cd tools/capacity-probe && bash probe.sh --window=5h {{ARGS}}

# Capacity probe: 7d only.
probe-7d *ARGS:
    cd tools/capacity-probe && bash probe.sh --window=7d {{ARGS}}

# Capacity probe: dry-run. Exercises every code path with `echo` replacing `claude`.
probe-dry *ARGS:
    cd tools/capacity-probe && bash probe.sh --dry-run {{ARGS}}

# Regenerate report.md from an existing capacity-probe run directory.
probe-report RUN_DIR:
    python3 tools/capacity-probe/report.py {{RUN_DIR}}

# Recompute and print low/mid/high quota bounds from an existing probe run.
probe-bounds RUN_DIR:
    python3 tools/capacity-probe/report.py --print-bounds {{RUN_DIR}}
