#!/usr/bin/env bash
# Provision GHunt into the Python tools directory and prepare its cookie store.
# GHunt requires authenticated Google cookies; place them at the path referenced
# by GHUNT_COOKIES_PATH (.env). This script only installs the tool.
set -euo pipefail

TOOLS_DIR="${TOOLS_DIR:-./tools/python}"
GHUNT_DIR="${TOOLS_DIR}/ghunt"

echo "[setup_ghunt] Installing GHunt into ${GHUNT_DIR}"
mkdir -p "${GHUNT_DIR}"

if [ ! -d "${GHUNT_DIR}/.git" ]; then
  git clone --depth 1 https://github.com/mxrch/GHunt.git "${GHUNT_DIR}"
fi

python3 -m pip install --upgrade pip
python3 -m pip install -r "${GHUNT_DIR}/requirements.txt" || true
python3 -m pip install ghunt || true

echo "[setup_ghunt] Done."
echo "Next: run 'ghunt login' and export cookies to the path in GHUNT_COOKIES_PATH."
