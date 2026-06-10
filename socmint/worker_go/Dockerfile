# SOCMINT Go tool worker. Includes the Go toolchain to compile the OSINT
# binaries plus a Python runtime to host the Celery adapters that wrap them.
FROM golang:1.22-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    GO_TOOLS_DIR=/app/tools/go

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv git curl tor \
    && rm -rf /var/lib/apt/lists/*

# Use an isolated venv to avoid PEP 668 "externally managed" restrictions.
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY worker_go/requirements.txt /app/worker_go/requirements.txt
RUN pip install --no-cache-dir -r /app/worker_go/requirements.txt

# ---------------------------------------------------------------------------
# Compile the Go OSINT binaries into $GO_TOOLS_DIR. `go install` names each
# binary after the module's final path segment, so we build into a temporary
# GOBIN and then rename to the exact filename each adapter expects
# (enola, DetectDee, gowitness, githound, ...). Builds are fault-tolerant: a
# failed build leaves the binary absent and the adapter degrades to
# 'unavailable' at runtime instead of breaking the image build.
#
# GOTOOLCHAIN=auto lets `go` auto-fetch a newer toolchain when a module
# declares a higher minimum (e.g. enola needs go>=1.26.3 and gowitness
# needs go>=1.26, while the base image ships go1.22).
# ---------------------------------------------------------------------------
ENV GOBIN=/tmp/gobin \
    GOTOOLCHAIN=auto
RUN set -e; mkdir -p "$GO_TOOLS_DIR" "$GOBIN"; \
    build_tool() { \
        module="$1"; produced="$2"; want="$3"; \
        echo "== go install $module =="; \
        if go install "$module"; then \
            if [ -f "$GOBIN/$produced" ]; then \
                mv -f "$GOBIN/$produced" "$GO_TOOLS_DIR/$want"; \
                chmod +x "$GO_TOOLS_DIR/$want"; \
                echo "  built $want"; \
            else \
                echo "  WARN: expected binary $produced not found for $module"; \
            fi; \
        else \
            echo "  WARN: failed to build $module (will degrade to unavailable)"; \
        fi; \
    }; \
    build_clone() { \
        repo="$1"; want="$2"; \
        echo "== git clone + go build $repo =="; \
        src="/tmp/src-$want"; rm -rf "$src"; \
        if git clone --depth 1 "$repo" "$src" \
            && ( cd "$src" && go build -o "$GO_TOOLS_DIR/$want" . ); then \
            chmod +x "$GO_TOOLS_DIR/$want"; \
            if [ -f "$src/data.json" ]; then \
                cp -f "$src/data.json" "$GO_TOOLS_DIR/$want.data.json"; \
                echo "  bundled $want.data.json"; \
            fi; \
            echo "  built $want"; \
        else \
            echo "  WARN: failed to build $want from $repo (will degrade to unavailable)"; \
        fi; \
        rm -rf "$src"; \
    }; \
    build_tool github.com/theyahya/enola/cmd/enola@latest enola          enola; \
    build_tool github.com/sensepost/gowitness@latest      gowitness      gowitness; \
    build_tool github.com/tillson/git-hound@latest        git-hound      githound; \
    build_tool github.com/dsonbaker/email2whatsapp@latest email2whatsapp email2whatsapp; \
    build_clone https://github.com/piaolin/DetectDee       DetectDee; \
    true

COPY worker_go /app/worker_go
COPY api /app/api

CMD ["celery", "-A", "worker_go.celery_app", "worker", "--loglevel=info", "--concurrency=2"]
