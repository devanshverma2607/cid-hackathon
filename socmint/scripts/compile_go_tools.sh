#!/usr/bin/env bash
# Compile the Go OSINT binaries into worker_go/tools/go so the Go-worker
# adapters can invoke them as subprocesses.
set -euo pipefail

OUT_DIR="${OUT_DIR:-./worker_go/tools/go}"
mkdir -p "${OUT_DIR}"

echo "[compile_go_tools] Output directory: ${OUT_DIR}"

# tool_name -> go install module path
declare -A TOOLS=(
  ["enola"]="github.com/TheYahya/enola@latest"
  ["detectdee"]="github.com/piec/detectdee@latest"
  ["gowitness"]="github.com/sensepost/gowitness@latest"
  ["githound"]="github.com/tillson/git-hound@latest"
)

export GOBIN="$(cd "${OUT_DIR}" && pwd)"

for name in "${!TOOLS[@]}"; do
  module="${TOOLS[$name]}"
  echo "[compile_go_tools] Building ${name} from ${module}"
  go install "${module}" || echo "  WARN: failed to build ${name}; continuing"
done

echo "[compile_go_tools] Binaries available in ${OUT_DIR}:"
ls -1 "${OUT_DIR}" || true
