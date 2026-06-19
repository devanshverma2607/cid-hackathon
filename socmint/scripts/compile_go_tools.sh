#!/usr/bin/env bash
# Compile the Go OSINT binaries into worker_go/tools/go so the Go-worker
# adapters can invoke them as subprocesses.
#
# This mirrors the authoritative build performed in worker_go/Dockerfile. It is
# only needed when running the worker outside Docker; `docker compose build`
# already produces these binaries. `go install` names each binary after the
# module's final path segment, so we build into a temporary GOBIN and rename to
# the exact filename each adapter's go_binary() lookup expects. GOTOOLCHAIN=auto
# lets `go` auto-fetch a newer toolchain when a module declares a higher minimum
# (enola/gowitness require go>=1.26). Builds are fault-tolerant: a failed build
# leaves the binary absent and the adapter degrades to 'unavailable' at runtime.
#
# NOTE: mailsleuth is intentionally NOT built here — it is a closed-source tool
# with no public Go module and is implemented key-lessly in
# worker_go/adapters/mailsleuth.py (no binary required).
set -euo pipefail

OUT_DIR="${OUT_DIR:-./worker_go/tools/go}"
mkdir -p "${OUT_DIR}"
echo "[compile_go_tools] Output directory: ${OUT_DIR}"
go version || { echo "ERROR: Go toolchain not found on PATH"; exit 1; }

GOBIN_TMP="$(mktemp -d)"
export GOBIN="${GOBIN_TMP}"
export GOTOOLCHAIN="${GOTOOLCHAIN:-auto}"
OUT_ABS="$(cd "${OUT_DIR}" && pwd)"

# module path -> (produced binary name) -> (wanted filename)
build_tool() {
  local module="$1" produced="$2" want="$3"
  echo "[compile_go_tools] go install ${module}"
  if go install "${module}"; then
    if [ -f "${GOBIN}/${produced}" ]; then
      mv -f "${GOBIN}/${produced}" "${OUT_ABS}/${want}"
      chmod +x "${OUT_ABS}/${want}"
      echo "  built ${want}"
    else
      echo "  WARN: expected binary ${produced} not found for ${module}"
    fi
  else
    echo "  WARN: failed to build ${module}; continuing"
  fi
}

# Some tools ship no installable cmd entrypoint (or need a bundled data file);
# clone + `go build .` and copy any sibling data.json next to the binary.
build_clone() {
  local repo="$1" want="$2"
  echo "[compile_go_tools] git clone + go build ${repo}"
  local src; src="$(mktemp -d)"
  if git clone --depth 1 "${repo}" "${src}" \
      && ( cd "${src}" && go build -o "${OUT_ABS}/${want}" . ); then
    chmod +x "${OUT_ABS}/${want}"
    [ -f "${src}/data.json" ] && cp -f "${src}/data.json" "${OUT_ABS}/${want}.data.json" \
      && echo "  bundled ${want}.data.json"
    echo "  built ${want}"
  else
    echo "  WARN: failed to build ${want} from ${repo}; continuing"
  fi
  rm -rf "${src}"
}

build_tool github.com/theyahya/enola/cmd/enola@latest enola          enola
build_tool github.com/sensepost/gowitness@latest      gowitness      gowitness
build_tool github.com/tillson/git-hound@latest        git-hound      githound
build_tool github.com/dsonbaker/email2whatsapp@latest email2whatsapp email2whatsapp
build_clone https://github.com/piaolin/DetectDee       DetectDee

rm -rf "${GOBIN_TMP}"
echo "[compile_go_tools] Binaries available in ${OUT_DIR}:"
ls -1 "${OUT_DIR}" || true
