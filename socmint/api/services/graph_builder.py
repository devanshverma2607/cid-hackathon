"""MODULE 7 — Graph Builder.

Writes/reads the identity graph in Neo4j and exports a Plotly-ready dict for the
dashboard. Node types: Identity, Account, Email, Username, Phone, Domain. Edge
types: LINKED_TO, USES, HAS_EMAIL, SAME_AS, OWNS_DOMAIN, LINKED_PHONE,
REUSES_CRED. See MODULE 7 (Section 5) of SOCMINT_PLAN_v2_0.txt.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
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

    def upsert_interaction_edge(
        self,
        case_id: str,
        subject_url: str,
        target_username: str,
        platform: str,
        count: int = 1,
    ) -> None:
        """Create or update an INTERACTS_WITH edge (SDM network graph).

        Records how often ``subject_url`` @-mentions / replies to
        ``target_username`` on ``platform``.  The target is merged as a
        lightweight Username node so it doesn't pollute the Account namespace
        until confirmed by the main pipeline.
        """
        with get_driver().session() as session:
            session.run(
                """
                MERGE (a:Account {url: $subject_url})
                MERGE (t:Username {value: $target_username})
                  ON CREATE SET t.platform = $platform
                MERGE (a)-[r:INTERACTS_WITH]->(t)
                SET r.count = $count,
                    r.platform = $platform,
                    r.case_id = $case_id
                """,
                subject_url=subject_url,
                target_username=target_username,
                platform=platform,
                count=count,
                case_id=str(case_id),
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
        self, case_id: UUID, max_nodes: int = 50, include_pivots: bool = True,
        include_interactions: bool = False,
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
                            "community": node.get("community"),
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
                            "community": None,
                        }
                    if ident_id not in nodes:
                        nodes[ident_id] = {
                            "id": ident_id,
                            "label": ident_val,
                            "platform": ident_kind,
                            "kind": ident_kind,
                            "url": None,
                            "confidence": 0,
                            "community": None,
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

            # --- SDM INTERACTS_WITH edges ---
            if include_interactions:
                interactions = session.run(
                    """
                    MATCH (a:Account)-[r:INTERACTS_WITH {case_id: $case_id}]->(t:Username)
                    RETURN a, t, r
                    LIMIT $limit
                    """,
                    case_id=str(case_id),
                    limit=max_nodes,
                )
                for record in interactions:
                    a = record["a"]
                    t = record["t"]
                    r = record["r"]
                    a_id = a.get("url")
                    t_id = f"username:{t.get('value')}"
                    if a_id and a_id not in nodes:
                        nodes[a_id] = {
                            "id": a_id, "label": a.get("username") or a_id,
                            "platform": a.get("platform", "unknown"),
                            "kind": "account", "url": a_id,
                            "confidence": 0, "community": None,
                        }
                    if t_id not in nodes:
                        nodes[t_id] = {
                            "id": t_id, "label": t.get("value", ""),
                            "platform": r.get("platform", "unknown"),
                            "kind": "username", "url": None,
                            "confidence": 0, "community": None,
                        }
                    edges.append({
                        "source": a_id, "target": t_id,
                        "confidence": 0, "tier": "INTERACTION",
                        "kind": "interacts_with",
                        "via_tool": f"sdm ({r.get('count', 0)} interactions)",
                    })
        return {"nodes": list(nodes.values()), "edges": edges}

    # --------------------------------------------------- community detection
    def detect_communities(self, case_id: UUID, write_back: bool = True) -> dict:
        """Partition the case's SAME_AS account graph into communities.

        Prefers Neo4j GDS Louvain when the plugin is installed; otherwise falls
        back to a dependency-free weighted label-propagation pass over the same
        edges. Communities refine persona clustering — a single persona can split
        into tightly-knit sub-clusters (e.g. work vs personal handles). Results
        are written back onto Account nodes (``a.community``) for the graph view.
        """
        nodes, edges = self._load_same_as(case_id)
        if not nodes:
            return {"method": "none", "community_count": 0, "communities": {}, "summaries": []}

        communities = self._gds_louvain(case_id)
        method = "gds_louvain"
        if not communities:
            communities = self._label_propagation(nodes, edges)
            method = "label_propagation"

        groups: dict[int, list[str]] = defaultdict(list)
        for node, comm in communities.items():
            groups[comm].append(node)
        summaries = [
            {"community_id": comm, "size": len(members), "members": sorted(members)[:25]}
            for comm, members in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        ]

        if write_back:
            try:
                self._write_communities(case_id, communities, method)
            except Exception as exc:  # noqa: BLE001
                logger.warning("community write-back failed: %s", exc)

        return {
            "method": method,
            "community_count": len(groups),
            "communities": communities,
            "summaries": summaries,
        }

    def _load_same_as(self, case_id: UUID):
        """Return (nodes:set, edges:list[(a,b,weight)]) of the case SAME_AS graph."""
        nodes: set[str] = set()
        edges: list[tuple[str, str, float]] = []
        with get_driver().session() as session:
            records = session.run(
                """
                MATCH (a:Account)-[r:SAME_AS {case_id: $cid}]->(b:Account)
                RETURN a.url AS a, b.url AS b, coalesce(r.confidence_score, 1.0) AS w
                """,
                cid=str(case_id),
            )
            for rec in records:
                a, b = rec["a"], rec["b"]
                if not a or not b or a == b:
                    continue
                nodes.add(a)
                nodes.add(b)
                edges.append((a, b, float(rec["w"] or 1.0)))
        return nodes, edges

    @staticmethod
    def _label_propagation(nodes, edges, max_iter: int = 25) -> dict:
        """Deterministic weighted label propagation (no external dependency)."""
        adj: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for a, b, w in edges:
            adj[a].append((b, w))
            adj[b].append((a, w))
        ordered = sorted(nodes)
        labels = {n: i for i, n in enumerate(ordered)}
        for _ in range(max_iter):
            changed = False
            for n in ordered:
                neigh = adj.get(n)
                if not neigh:
                    continue
                tally: dict[int, float] = defaultdict(float)
                for nb, w in neigh:
                    tally[labels[nb]] += w
                # highest neighbour weight; tie-break to smallest label id
                best = min(tally.items(), key=lambda kv: (-kv[1], kv[0]))[0]
                if labels[n] != best:
                    labels[n] = best
                    changed = True
            if not changed:
                break
        renum: dict[int, int] = {}
        for n in ordered:
            lab = labels[n]
            if lab not in renum:
                renum[lab] = len(renum)
        return {n: renum[labels[n]] for n in ordered}

    def _gds_louvain(self, case_id: UUID):
        """Try Neo4j GDS Louvain; return {url: community_id} or None if unavailable."""
        graph_name = "comm_" + str(case_id).replace("-", "")
        cid = str(case_id)
        try:
            with get_driver().session() as session:
                session.run("CALL gds.graph.drop($g, false) YIELD graphName", g=graph_name)
                session.run(
                    """
                    CALL gds.graph.project.cypher(
                      $g,
                      'MATCH (n:Account) WHERE (n)-[:SAME_AS {case_id:$cid}]-() RETURN id(n) AS id',
                      'MATCH (a:Account)-[r:SAME_AS]-(b:Account) WHERE r.case_id = $cid
                       RETURN id(a) AS source, id(b) AS target, coalesce(r.confidence_score,1.0) AS weight',
                      {parameters: {cid: $cid}}
                    ) YIELD graphName
                    """,
                    g=graph_name, cid=cid,
                )
                result = session.run(
                    """
                    CALL gds.louvain.stream($g, {relationshipWeightProperty: 'weight'})
                    YIELD nodeId, communityId
                    RETURN gds.util.asNode(nodeId).url AS url, communityId
                    """,
                    g=graph_name,
                )
                communities: dict[str, int] = {}
                seen: dict[int, int] = {}
                for rec in result:
                    url = rec["url"]
                    if not url:
                        continue
                    raw = int(rec["communityId"])
                    if raw not in seen:
                        seen[raw] = len(seen)
                    communities[url] = seen[raw]
                session.run("CALL gds.graph.drop($g, false) YIELD graphName", g=graph_name)
            return communities or None
        except Exception as exc:  # noqa: BLE001 — GDS optional; fall back
            logger.debug("GDS louvain unavailable (%s); using label propagation", exc)
            return None

    def _write_communities(self, case_id: UUID, communities: dict, method: str) -> None:
        """Annotate Account nodes with their detected community id."""
        rows = [{"url": url, "c": int(comm)} for url, comm in communities.items()]
        if not rows:
            return
        with get_driver().session() as session:
            session.run(
                """
                UNWIND $rows AS row
                MATCH (a:Account {url: row.url})
                SET a.community = row.c, a.community_method = $method, a.community_case = $cid
                """,
                rows=rows, method=method, cid=str(case_id),
            )
