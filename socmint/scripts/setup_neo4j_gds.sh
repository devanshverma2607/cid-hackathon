#!/usr/bin/env bash
# Provision the Neo4j Graph Data Science (GDS) plugin JAR for offline use.
#
# The docker-compose `neo4j` service bind-mounts ./neo4j/plugins into the
# container's /plugins directory. Neo4j's built-in NEO4J_PLUGINS auto-downloader
# fetches the JAR from https://graphdatascience.ninja at startup, which fails in
# network-restricted / air-gapped deployments. This script downloads the JAR
# version that matches the running Neo4j build directly, so GDS Louvain is
# available without the runtime CDN call. If the JAR is absent, the GraphBuilder
# degrades to its in-process label-propagation fallback (correct, just slower on
# very large graphs), so this step is an enhancement, not a hard requirement.
set -euo pipefail

PLUGINS_DIR="${PLUGINS_DIR:-./neo4j/plugins}"
# Must match the GDS<->Neo4j compatibility matrix (https://graphdatascience.ninja/versions.json).
# Neo4j 5.26.x (current pinned image) -> GDS 2.13.x.
GDS_VERSION="${GDS_VERSION:-2.13.10}"
JAR="neo4j-graph-data-science-${GDS_VERSION}.jar"
URL="https://graphdatascience.ninja/${JAR}"

mkdir -p "${PLUGINS_DIR}"

if [ -f "${PLUGINS_DIR}/${JAR}" ]; then
  echo "[setup_neo4j_gds] ${JAR} already present in ${PLUGINS_DIR}; nothing to do."
  exit 0
fi

echo "[setup_neo4j_gds] Downloading ${JAR} into ${PLUGINS_DIR}"
if command -v curl >/dev/null 2>&1; then
  curl -fSL "${URL}" -o "${PLUGINS_DIR}/${JAR}"
elif command -v wget >/dev/null 2>&1; then
  wget -O "${PLUGINS_DIR}/${JAR}" "${URL}"
else
  echo "[setup_neo4j_gds] ERROR: need curl or wget to download the GDS JAR." >&2
  exit 1
fi

echo "[setup_neo4j_gds] Done. Recreate Neo4j to load the plugin:"
echo "  docker compose up -d --force-recreate neo4j"
