# SOCMINT Python tool worker.
# Build context is the repository root so both the worker_python and api
# packages are importable. OSINT CLI tools are provisioned separately into
# /tools/python and called as subprocesses.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=10

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential git curl tor \
    && rm -rf /var/lib/apt/lists/*

# Install CPU-only torch first so sentence-transformers reuses it instead of
# pulling the multi-gigabyte CUDA/GPU wheel stack.
RUN pip install --no-cache-dir torch==2.4.1 --index-url https://download.pytorch.org/whl/cpu

COPY worker_python/requirements.txt /app/worker_python/requirements.txt
RUN pip install --no-cache-dir -r /app/worker_python/requirements.txt

# ---------------------------------------------------------------------------
# OSINT tool provisioning (no-API-key + credentialed pip/pipx tools).
# Each PyPI console-script tool is installed in its OWN isolated venv via pipx
# (matching the plan's "isolated venv per tool" requirement) with its CLI binary
# exposed on PATH at /usr/local/bin, where the adapters locate it via
# shutil.which(). Installs are fault-tolerant so a single broken tool never
# fails the image build; missing tools simply degrade to 'unavailable' evidence
# at runtime.
# ---------------------------------------------------------------------------
ENV PIPX_HOME=/opt/pipx \
    PIPX_BIN_DIR=/usr/local/bin
RUN pip install --no-cache-dir pipx && \
    for tool in \
        holehe \
        socialscan \
        maigret \
        sherlock-project \
        waybackpy \
        h8mail \
        theHarvester \
        finalrecon \
        toutatis \
        ghunt \
        nexfil \
        ignorant \
        sublist3r \
        dnstwist \
    ; do \
        echo "== pipx install $tool ==" && \
        pipx install "$tool" || echo "WARN: failed to install $tool (will degrade to unavailable)"; \
    done

# ---------------------------------------------------------------------------
# Git-distributed "python <script>.py" OSINT tools. These are cloned, one repo
# per directory, under /tools/python/<repo>. The ToolAdapter base class locates
# each entry-script here at runtime and executes it from inside its own repo
# (so the tool's relative imports/data files resolve). Per-repo Python deps are
# installed into the shared interpreter; failures are tolerated so the rest of
# the image still builds.
# ---------------------------------------------------------------------------
ENV PY_TOOLS_DIR=/tools/python
RUN set -e; mkdir -p "$PY_TOOLS_DIR"; \
    clone_tool() { \
        url="$1"; dir="$2"; \
        echo "== clone $dir =="; \
        if git clone --depth 1 "$url" "$PY_TOOLS_DIR/$dir"; then \
            if [ -f "$PY_TOOLS_DIR/$dir/requirements.txt" ]; then \
                pip install --no-cache-dir -r "$PY_TOOLS_DIR/$dir/requirements.txt" \
                    || echo "WARN: deps failed for $dir (may still run)"; \
            fi; \
        else \
            echo "WARN: clone failed for $dir (will degrade to unavailable)"; \
        fi; \
    }; \
    clone_tool https://github.com/p1ngul1n0/blackbird            blackbird; \
    clone_tool https://github.com/WebBreacher/WhatsMyName          whatsmyname; \
    clone_tool https://github.com/AdrienLF/Zehef                   zehef; \
    clone_tool https://github.com/BullsEye0/dorks-eye             dorks_eye; \
    clone_tool https://github.com/N0rz3/Eyes                       eyes; \
    clone_tool https://github.com/megadose/HashTray                hashtray; \
    clone_tool https://github.com/N0rz3/Tracer                     tracer; \
    true

# ---------------------------------------------------------------------------
# Telethon — MTProto client library used by the TelegramIntelAdapter for live
# Telegram username resolution. Installed as its own late layer (rather than in
# requirements.txt) so adding it does not invalidate the cached pipx/git-clone
# tool layers above.
# ---------------------------------------------------------------------------
RUN pip install --no-cache-dir telethon==1.36.0

# ---------------------------------------------------------------------------
# Tier-1 tool additions: phone intelligence + structured-ID extraction.
#   * socid_extractor — imported by SocidExtractorAdapter to turn a profile URL
#     into a structured account record (alt usernames, internal IDs, links).
#     Installed as its own late pip layer so it never invalidates the cached
#     pipx/git-clone tool layers above; a failed install degrades to unavailable.
#   * PhoneInfoga — phone-number recon framework. Its keyless local scanner runs
#     without any API key; the pinned release binary is placed on PATH where the
#     adapter finds it via shutil.which(). A failed fetch degrades to unavailable.
# (Pillow + ImageHash, used by the profile-photo pHash service, and phonenumbers,
#  used by the offline phone-enrichment adapter, are already in requirements.txt.)
# ---------------------------------------------------------------------------
RUN pip install --no-cache-dir socid-extractor \
    || echo "WARN: socid-extractor install failed (adapter will degrade to unavailable)"

RUN set -eux; \
    pif_ver="v2.11.0"; \
    case "$(uname -m)" in \
        x86_64) pif_arch="x86_64";; \
        aarch64|arm64) pif_arch="arm64";; \
        *) pif_arch="x86_64";; \
    esac; \
    url="https://github.com/sundowndev/phoneinfoga/releases/download/${pif_ver}/phoneinfoga_Linux_${pif_arch}.tar.gz"; \
    ( curl -fsSL "$url" -o /tmp/phoneinfoga.tar.gz \
        && tar -xzf /tmp/phoneinfoga.tar.gz -C /usr/local/bin phoneinfoga \
        && chmod +x /usr/local/bin/phoneinfoga \
        && rm -f /tmp/phoneinfoga.tar.gz \
        && phoneinfoga version ) \
        || echo "WARN: phoneinfoga install failed (will degrade to unavailable)"

# SOCKS backend so httpx can route the keyless passive-recon adapters through
# the Tor sidecar (TOR_PROXY=socks5://tor:9050); adapters fall back to direct
# egress automatically when Tor is unavailable.
RUN pip install --no-cache-dir socksio==1.0.0

COPY worker_python /app/worker_python
COPY api /app/api

CMD ["celery", "-A", "worker_python.celery_app", "worker", "--loglevel=info", "--concurrency=4"]
