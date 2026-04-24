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

login:
    #!/usr/bin/env bash
    set -euo pipefail
    DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/cc-nerf-buster}"
    CA_CERT="$DATA_DIR/ca.crt"
    CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$DATA_DIR/claude-config}"
    PORT=9480
    if [[ ! -f "$CA_CERT" ]]; then
        echo "Missing CA cert at $CA_CERT. Run 'just install' first." >&2
        exit 1
    fi
    mkdir -p "$CLAUDE_CONFIG_DIR"
    export CLAUDE_CONFIG_DIR
    export https_proxy="http://localhost:$PORT"
    export HTTPS_PROXY="http://localhost:$PORT"
    export http_proxy="http://localhost:$PORT"
    export HTTP_PROXY="http://localhost:$PORT"
    export NODE_EXTRA_CA_CERTS="$CA_CERT"
    export SSL_CERT_FILE="$CA_CERT"
    export CURL_CA_BUNDLE="$CA_CERT"
    export REQUESTS_CA_BUNDLE="$CA_CERT"
    export GIT_SSL_CAINFO="$CA_CERT"
    exec claude

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
