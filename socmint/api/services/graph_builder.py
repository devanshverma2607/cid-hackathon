"""MODULE 7 — Graph Builder.

Writes/reads the identity graph in Neo4j and exports a Plotly-ready dict for the
dashboard. Node types: Identity, Account, Email, Username, Phone, Domain. Edge
types: LINKED_TO, USES, HAS_EMAIL, SAME_AS, OWNS_DOMAIN, LINKED_PHONE,
REUSES_CRED. See MODULE 7 (Section 5) of SOCMINT_PLAN_v2_0.txt.
"""
from __future__ import annotations

import json
import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from api.db.neo4j import get_driver
from api.models.identity_link import IdentityLink

logger = logging.getLogger(__name__)


class GraphBuilder:
    """Build and query the Neo4j identity graph."""

    def upsert_account_node(self, platform: str, url: str, username: str, metadata: dict) -> None:
        """Create or update an Account node."""
        with get_driver().session() as session:
            session.run(
                """
                MERGE (a:Account {url: $url})
                SET a.platform = $platform,
                    a.username = $username,
                    a.metadata = $metadata
                """,
                url=url,
                platform=platform,
                username=username,
                metadata=json.dumps(metadata or {}),
            )

    def upsert_identity_link(self, link: IdentityLink) -> None:
        """Create a SAME_AS edge between two Account nodes."""
        with get_driver().session() as session:
            session.run(
                """
                MERGE (a:Account {url: $account_a})
                  ON CREATE SET a.platform = $platform_a
                MERGE (b:Account {url: $account_b})
                  ON CREATE SET b.platform = $platform_b
                MERGE (a)-[r:SAME_AS]->(b)
                SET r.confidence_score = $confidence_score,
                    r.confidence_tier = $confidence_tier,
                    r.signal_breakdown = $signal_breakdown,
                    r.signal_count = $signal_count,
                    r.case_id = $case_id
                """,
                account_a=link.account_a,
                account_b=link.account_b,
                platform_a=link.platform_a,
                platform_b=link.platform_b,
                confidence_score=link.confidence_score,
                confidence_tier=link.confidence_tier,
                signal_breakdown=json.dumps(link.signal_breakdown),
                signal_count=link.signal_count,
                case_id=str(link.case_id),
            )

    def upsert_pivot_edge(
        self,
        case_id,
        via_platform: str,
        via_tool: str,
        seed_type: str,
        seed_value: str,
    ) -> None:
        """Record a DISCOVERED edge: a tool surfaced a new identifier.

        Captures the cross-tool reasoning chain (the "brain") in the graph —
        e.g. (Account:github)-[:DISCOVERED {tool:'github_api'}]->(Email). The
        identifier node is typed (Email/Phone/Username/Domain) for the dashboard.
        """
        label = {
            "email": "Email",
            "phone": "Phone",
            "username": "Username",
            "domain": "Domain",
        }.get(seed_type, "Identifier")
        with get_driver().session() as session:
            session.run(
                f"""
                MERGE (src:Source {{name: $via_platform}})
                MERGE (ident:{label} {{value: $seed_value}})
                MERGE (src)-[r:DISCOVERED]->(ident)
                SET r.via_tool = $via_tool,
                    r.case_id = $case_id,
                    r.seed_type = $seed_type
                """,
                via_platform=via_platform or "seed",
                seed_value=seed_value,
                via_tool=via_tool or "unknown",
                case_id=str(case_id),
                seed_type=seed_type,
            )


    def build_graph_from_case(self, case_id: UUID, session: Session) -> None:
        """Load all evidence units + identity links and build the graph."""
        units = session.execute(
            text(
                "SELECT source_platform, result_value, result_type, platform_enrichment "
                "FROM evidence_units WHERE case_id = :cid AND result_type = 'account_found'"
            ),
            {"cid": str(case_id)},
        ).mappings().all()
        for row in units:
            enrichment = row["platform_enrichment"] or {}
            if isinstance(enrichment, str):
                try:
                    enrichment = json.loads(enrichment)
                except json.JSONDecodeError:
                    enrichment = {}
            self.upsert_account_node(
                platform=row["source_platform"],
                url=row["result_value"],
                username=enrichment.get("username", ""),
                metadata=enrichment,
            )

        links = session.execute(
            text("SELECT * FROM identity_links WHERE case_id = :cid"),
            {"cid": str(case_id)},
        ).mappings().all()
        for row in links:
            breakdown = row["signal_breakdown"]
            if isinstance(breakdown, str):
                try:
                    breakdown = json.loads(breakdown)
                except json.JSONDecodeError:
                    breakdown = {}
            link = IdentityLink(
                link_id=row["link_id"],
                case_id=row["case_id"],
                account_a=row["account_a"],
                account_b=row["account_b"],
                platform_a=row["platform_a"],
                platform_b=row["platform_b"],
                confidence_score=row["confidence_score"],
                confidence_tier=row["confidence_tier"],
                signal_breakdown=breakdown,
                signal_count=row["signal_count"],
            )
            self.upsert_identity_link(link)

    def export_graph_for_plotly(
        self, case_id: UUID, max_nodes: int = 50, include_pivots: bool = True
    ) -> dict:
        """Return {nodes, edges} for a case: SAME_AS links plus pivot DISCOVERED edges."""
        nodes: dict[str, dict] = {}
        edges: list[dict] = []
        with get_driver().session() as session:
            records = session.run(
                """
                MATCH (a:Account)-[r:SAME_AS {case_id: $case_id}]->(b:Account)
                RETURN a, b, r
                ORDER BY r.confidence_score DESC
                LIMIT $limit
                """,
                case_id=str(case_id),
                limit=max_nodes,
            )
            for record in records:
                a = record["a"]
                b = record["b"]
                r = record["r"]
                for node in (a, b):
                    nid = node.get("url")
                    if nid and nid not in nodes:
                        nodes[nid] = {
                            "id": nid,
                            "label": node.get("username") or nid,
                            "platform": node.get("platform", "unknown"),
                            "kind": "account",
                            "url": nid,
                            "confidence": r.get("confidence_score", 0),
                        }
                edges.append(
                    {
                        "source": a.get("url"),
                        "target": b.get("url"),
                        "confidence": r.get("confidence_score", 0),
                        "tier": r.get("confidence_tier", "LOW"),
                        "kind": "same_as",
                        "signals": r.get("signal_breakdown", "{}"),
                    }
                )

            if include_pivots:
                pivots = session.run(
                    """
                    MATCH (src:Source)-[r:DISCOVERED {case_id: $case_id}]->(ident)
                    RETURN src, ident, r, labels(ident) AS ident_labels
                    LIMIT $limit
                    """,
                    case_id=str(case_id),
                    limit=max_nodes,
                )
                for record in pivots:
                    src = record["src"]
                    ident = record["ident"]
                    r = record["r"]
                    ident_labels = record["ident_labels"] or ["Identifier"]
                    ident_kind = (ident_labels[0] if ident_labels else "Identifier").lower()
                    src_name = src.get("name")
                    ident_val = ident.get("value")
                    if not src_name or not ident_val:
                        continue
                    src_id = f"source:{src_name}"
                    ident_id = f"{ident_kind}:{ident_val}"
                    if src_id not in nodes:
                        nodes[src_id] = {
                            "id": src_id,
                            "label": src_name,
                            "platform": src_name,
                            "kind": "source",
                            "url": None,
                            "confidence": 0,
                        }
                    if ident_id not in nodes:
                        nodes[ident_id] = {
                            "id": ident_id,
                            "label": ident_val,
                            "platform": ident_kind,
                            "kind": ident_kind,
                            "url": None,
                            "confidence": 0,
                        }
                    edges.append(
                        {
                            "source": src_id,
                            "target": ident_id,
                            "confidence": 0,
                            "tier": "PIVOT",
                            "kind": "discovered",
                            "via_tool": r.get("via_tool", ""),
                        }
                    )
        return {"nodes": list(nodes.values()), "edges": edges}
