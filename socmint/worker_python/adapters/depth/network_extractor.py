"""SDM — Network Extractor: builds interaction graphs from depth adapter output.

Operates on the interaction dicts already collected by per-platform depth
adapters.  Writes INTERACTS_WITH edges to Neo4j and emits pivot seeds for
top interaction targets.
"""
from __future__ import annotations
import logging, os
from typing import Optional
from api.services.graph_builder import GraphBuilder

logger = logging.getLogger(__name__)

MAX_TARGETS = int(os.environ.get("SDM_MAX_INTERACTION_TARGETS", "20"))
MIN_COUNT = int(os.environ.get("SDM_MIN_INTERACTION_COUNT", "3"))


def build_interaction_graph(
    interactions: dict[str, int],
    platform: str,
    subject_url: str,
    case_id: str,
) -> dict:
    """Process raw interactions into a capped, sorted interaction graph.

    Returns {target: count} for the top MAX_TARGETS targets.
    Writes INTERACTS_WITH edges to Neo4j for each.
    """
    if not interactions:
        return {}
    sorted_targets = sorted(interactions.items(), key=lambda kv: -kv[1])[:MAX_TARGETS]
    graph = GraphBuilder()
    for target, count in sorted_targets:
        try:
            graph.upsert_interaction_edge(
                case_id=case_id,
                subject_url=subject_url,
                target_username=target,
                platform=platform,
                count=count,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("interaction edge write failed: %s", exc)
    return dict(sorted_targets)


def extract_pivot_seeds(
    interactions: dict[str, int],
    platform: str,
    via_tool: str = "network_extractor",
) -> list[dict]:
    """Return pivot seed candidates from interaction targets above threshold."""
    seeds = []
    for target, count in interactions.items():
        if count >= MIN_COUNT:
            seeds.append({
                "seed_type": "username",
                "seed_value": target.lstrip("@"),
                "via_tool": via_tool,
                "via_platform": platform,
                "source_value": target,
            })
    return seeds[:MAX_TARGETS]
