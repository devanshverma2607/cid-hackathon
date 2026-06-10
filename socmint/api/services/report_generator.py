"""MODULE 9 — Report Generator.

Assembles the JSON evidence package, renders a PDF via ReportLab, signs the
bundle with SHA-256, and stores outputs to MinIO and a local case directory.
See MODULE 9 (Section 5) of SOCMINT_PLAN_v2_0.txt.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from api.config import get_settings
from api.db import minio_client


class ReportGenerator:
    """Build, render, sign, and persist the evidence package."""

    def generate_json_package(self, case_id: UUID, session: Session) -> dict:
        """Assemble the structured, machine-readable evidence package."""
        case = session.execute(
            text("SELECT * FROM cases WHERE case_id = :cid"), {"cid": str(case_id)}
        ).mappings().first()

        evidence = session.execute(
            text("SELECT * FROM evidence_units WHERE case_id = :cid"), {"cid": str(case_id)}
        ).mappings().all()

        links = session.execute(
            text("SELECT * FROM identity_links WHERE case_id = :cid ORDER BY confidence_score DESC"),
            {"cid": str(case_id)},
        ).mappings().all()

        audit = session.execute(
            text("SELECT * FROM audit_log WHERE case_id = :cid ORDER BY created_at ASC"),
            {"cid": str(case_id)},
        ).mappings().all()

        def clean(rows):
            return [json.loads(json.dumps(dict(r), default=str)) for r in rows]

        confirmed_links = [
            link for link in clean(links) if link.get("analyst_decision") == "CONFIRMED"
        ]

        preservation = [
            {
                "evidence_id": str(e["evidence_id"]),
                "source_platform": e["source_platform"],
                "result_value": e["result_value"],
                "snapshot_ref": e["snapshot_ref"],
                "snapshot_hash": e["snapshot_hash"],
                "wayback_ref": e["wayback_ref"],
            }
            for e in evidence
            if e["snapshot_ref"] or e["wayback_ref"]
        ]

        # Synthesised intelligence assessment (ranked findings, profile, risk).
        from api.services.insight_engine import InsightEngine
        from api.services.persona_resolver import PersonaResolver
        from api.services.profile_engine import ProfileEngine

        clean_evidence = clean(evidence)
        clean_links = clean(links)
        clean_case = json.loads(json.dumps(dict(case), default=str)) if case else {}

        intelligence = InsightEngine().assess(clean_evidence, clean_links, clean_case)

        try:
            persona = PersonaResolver().resolve(case_id, session)
        except Exception:
            persona = None
        subject_dossier = ProfileEngine().build(
            clean_evidence, clean_links, clean_case, persona
        )

        return {
            "case": clean_case,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "intelligence_assessment": intelligence,
            "subject_dossier": subject_dossier,
            "summary": {
                "total_evidence_units": len(evidence),
                "total_identity_links": len(links),
                "confirmed_links": len(confirmed_links),
                "high": sum(1 for l in links if l["confidence_tier"] == "HIGH"),
                "medium": sum(1 for l in links if l["confidence_tier"] == "MEDIUM"),
                "low": sum(1 for l in links if l["confidence_tier"] == "LOW"),
            },
            "identity_links": clean(links),
            "confirmed_identity_links": confirmed_links,
            "evidence_units": clean(evidence),
            "preservation_references": preservation,
            "audit_log": clean(audit),
        }

    def _dossier_section(self, story, dossier: dict, styles) -> None:
        """Render the inferred subject dossier into the PDF story (in place)."""
        if not dossier:
            return
        from reportlab.lib import colors
        from reportlab.lib.units import cm
        from reportlab.platypus import Paragraph, Spacer, Table, TableStyle, PageBreak

        story.append(Paragraph("Subject Dossier (Inferred)", styles["Heading1"]))
        story.append(Paragraph(
            "<i>The following attributes are algorithmically inferred from collected "
            "open-source evidence and are graded by confidence. They are investigative "
            "leads, not established facts, and require analyst corroboration.</i>",
            styles["BodyText"],
        ))
        story.append(Spacer(1, 0.3 * cm))

        if dossier.get("summary"):
            story.append(Paragraph(dossier["summary"], styles["Normal"]))
            story.append(Spacer(1, 0.4 * cm))

        # Footprint + completeness snapshot.
        fp = dossier.get("footprint", {})
        comp = dossier.get("profile_completeness", {})
        snap_rows = [
            ["Footprint score", f"{fp.get('footprint_score', 0)}/100 ({fp.get('visibility', 'n/a')})"],
            ["Platforms present", str(fp.get("platform_count", 0))],
            ["Confirmed accounts", str(fp.get("confirmed_accounts", 0))],
            ["Profile completeness", f"{comp.get('score', 0)}%"],
            ["Distinct personas", str(dossier.get("persona_count", 0))],
        ]
        snap = Table(snap_rows, colWidths=[6 * cm, 9 * cm])
        snap.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
        ]))
        story.append(snap)
        story.append(Spacer(1, 0.4 * cm))

        # Inferred attributes with confidence.
        attrs = dossier.get("attributes", {})
        attr_rows = [["Attribute", "Inferred value(s)", "Conf."]]

        def _vals(items, n=3):
            out = []
            for it in (items or [])[:n]:
                if isinstance(it, dict):
                    out.append(str(it.get("value", "")))
                else:
                    out.append(str(it))
            return ", ".join(out)

        def _conf(items):
            for it in (items or []):
                if isinstance(it, dict) and it.get("confidence"):
                    return str(it["confidence"]).upper()
            return ""

        attr_specs = [
            ("Name", attrs.get("names")),
            ("Location", attrs.get("locations")),
            ("Occupation", attrs.get("occupation")),
            ("Languages", attrs.get("languages")),
            ("Affiliations", attrs.get("affiliations")),
            ("Websites", attrs.get("websites")),
        ]
        for label, items in attr_specs:
            if items:
                attr_rows.append([label, Paragraph(_vals(items), styles["BodyText"]), _conf(items)])
        if attrs.get("timezone"):
            attr_rows.append(["Timezone", str(attrs["timezone"]), ""])
        if attrs.get("phone_region"):
            attr_rows.append(["Phone region", str(attrs["phone_region"]), ""])

        if len(attr_rows) > 1:
            attr_table = Table(attr_rows, colWidths=[3.5 * cm, 9.5 * cm, 2 * cm])
            attr_table.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))
            story.append(Paragraph("Inferred Attributes", styles["Heading2"]))
            story.append(attr_table)
            story.append(Spacer(1, 0.4 * cm))

        # Behavioral fingerprint.
        bf = dossier.get("behavioral_fingerprint", {})
        if bf:
            story.append(Paragraph("Behavioral Fingerprint", styles["Heading2"]))
            bf_rows = [
                ["Dominant handle", str(bf.get("dominant_handle", "") or "n/a")],
                ["Handle consistency", f"{bf.get('handle_consistency', 0)}%"],
                ["Distinct handles", str(bf.get("distinct_handles", 0))],
                ["Naming traits", ", ".join(filter(None, [
                    "numeric suffix" if bf.get("uses_numeric_suffix") else "",
                    "leetspeak" if bf.get("uses_leet") else "",
                    (f"separator '{bf.get('preferred_separator')}'"
                     if bf.get("preferred_separator") else ""),
                ])) or "none detected"],
                ["Avatar reuse", f"{bf.get('avatar_reuse_count', 0)} reuse(s), "
                                 f"{bf.get('distinct_avatars', 0)} distinct"],
                ["Cross-platform consistency", f"{bf.get('cross_platform_consistency', 0)}%"],
            ]
            bf_table = Table(bf_rows, colWidths=[6 * cm, 9 * cm])
            bf_table.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
            ]))
            story.append(bf_table)
            if bf.get("style_notes"):
                for note in bf["style_notes"][:5]:
                    story.append(Paragraph(f"\u2022 {note}", styles["BodyText"]))
            story.append(Spacer(1, 0.4 * cm))

        # Temporal + interests.
        tmp = dossier.get("temporal", {})
        interests = dossier.get("interests", {})
        if tmp.get("active_span_days") is not None or interests.get("categories"):
            story.append(Paragraph("Activity & Interests", styles["Heading2"]))
            if tmp.get("earliest_activity"):
                story.append(Paragraph(
                    f"Active from <b>{str(tmp.get('earliest_activity', ''))[:10]}</b> to "
                    f"<b>{str(tmp.get('latest_activity', ''))[:10]}</b> "
                    f"(~{tmp.get('active_span_days', 0)} days; era: {tmp.get('active_era', 'n/a')}).",
                    styles["BodyText"],
                ))
            if interests.get("categories"):
                cats = ", ".join(
                    f"{c.get('label', c.get('category'))} ({c.get('platform_count')})"
                    if isinstance(c, dict) else str(c)
                    for c in interests["categories"][:6]
                )
                story.append(Paragraph(f"Interest categories: {cats}.", styles["BodyText"]))
            if interests.get("primary_interest"):
                story.append(Paragraph(
                    f"Primary interest: <b>{interests['primary_interest']}</b>.", styles["BodyText"],
                ))
            elif interests.get("top_interests"):
                story.append(Paragraph(
                    f"Top areas: <b>{', '.join(interests['top_interests'])}</b>.", styles["BodyText"],
                ))
            ts = interests.get("tech_sophistication", {})
            if ts.get("level"):
                story.append(Paragraph(
                    f"Technical sophistication: <b>{ts.get('level')}</b> \u2014 {ts.get('rationale', '')}",
                    styles["BodyText"],
                ))
            story.append(Spacer(1, 0.4 * cm))

        # Explainable reasoning chains.
        reasoning = dossier.get("reasoning", [])
        if reasoning:
            story.append(Paragraph("Reasoning & Evidence Basis", styles["Heading2"]))
            for r in reasoning[:12]:
                ev = "; ".join(str(x) for x in (r.get("evidence") or [])[:3])
                story.append(Paragraph(
                    f"\u2022 <b>{r.get('claim', '')}</b> "
                    f"<i>(confidence: {str(r.get('confidence', '')).upper()})</i><br/>"
                    f"<font size=7>Basis: {ev}</font>",
                    styles["BodyText"],
                ))
                story.append(Spacer(1, 0.1 * cm))

        story.append(PageBreak())

    def generate_pdf_report(self, case_id: UUID, json_package: dict) -> bytes:
        """Render a human-readable PDF evidence report via ReportLab."""
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.platypus import (
            Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle, PageBreak,
        )

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4, title=f"SOCMINT Report {case_id}")
        styles = getSampleStyleSheet()
        story = []

        case = json_package.get("case", {})
        summary = json_package.get("summary", {})

        # Cover page.
        story.append(Paragraph("SOCMINT — Suspect Profiling Report", styles["Title"]))
        story.append(Spacer(1, 0.5 * cm))
        story.append(Paragraph(f"Case ID: {case_id}", styles["Normal"]))
        story.append(Paragraph(f"Generated: {json_package.get('generated_at', '')}", styles["Normal"]))
        story.append(Paragraph(f"Analyst: {case.get('analyst_id', 'N/A')}", styles["Normal"]))
        story.append(Paragraph(f"Authority reference: {case.get('authority_id', 'N/A')}", styles["Normal"]))
        story.append(Paragraph(f"Agency: {case.get('agency_id', 'N/A')}", styles["Normal"]))
        story.append(PageBreak())

        # Executive summary.
        story.append(Paragraph("Executive Summary", styles["Heading1"]))
        summary_rows = [
            ["Seed type", case.get("seed_type", "")],
            ["Seed value", case.get("seed_value", "")],
            ["Evidence units", str(summary.get("total_evidence_units", 0))],
            ["Identity links", str(summary.get("total_identity_links", 0))],
            ["Confirmed links", str(summary.get("confirmed_links", 0))],
            ["HIGH / MEDIUM / LOW",
             f"{summary.get('high', 0)} / {summary.get('medium', 0)} / {summary.get('low', 0)}"],
        ]
        table = Table(summary_rows, colWidths=[6 * cm, 9 * cm])
        table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke),
        ]))
        story.append(table)
        story.append(Spacer(1, 0.5 * cm))

        # Intelligence assessment (narrative + risk + ranked findings).
        intel = json_package.get("intelligence_assessment", {})
        if intel:
            risk = intel.get("risk", {})
            story.append(Paragraph("Intelligence Assessment", styles["Heading1"]))
            story.append(Paragraph(
                f"Exposure: <b>{risk.get('band', 'N/A')}</b> ({risk.get('score', 0)}/100)",
                styles["Normal"],
            ))
            story.append(Spacer(1, 0.2 * cm))
            story.append(Paragraph(intel.get("narrative", ""), styles["Normal"]))
            story.append(Spacer(1, 0.4 * cm))

            findings = intel.get("key_findings", [])
            if findings:
                story.append(Paragraph("Key Findings", styles["Heading2"]))
                find_rows = [["Severity", "Finding", "Conf."]]
                for f in findings[:25]:
                    find_rows.append([
                        str(f.get("severity", "")).upper(),
                        Paragraph(str(f.get("title", "")), styles["BodyText"]),
                        str(f.get("confidence", "")),
                    ])
                find_table = Table(find_rows, colWidths=[2.2 * cm, 10.8 * cm, 2 * cm])
                find_table.setStyle(TableStyle([
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]))
                story.append(find_table)
                story.append(Spacer(1, 0.5 * cm))

            actions = intel.get("recommended_actions", [])
            if actions:
                story.append(Paragraph("Recommended Investigative Actions", styles["Heading2"]))
                act_rows = [["Priority", "Category", "Action"]]
                for a in actions[:20]:
                    act_rows.append([
                        str(a.get("priority", "")).upper(),
                        str(a.get("category", "")).replace("_", " "),
                        Paragraph(str(a.get("action", "")), styles["BodyText"]),
                    ])
                act_table = Table(act_rows, colWidths=[2 * cm, 3 * cm, 10 * cm])
                act_table.setStyle(TableStyle([
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]))
                story.append(act_table)
                story.append(Spacer(1, 0.5 * cm))
            story.append(PageBreak())

        # Subject dossier (inferred profile, fingerprint, reasoning).
        self._dossier_section(story, json_package.get("subject_dossier", {}), styles)

        # Identity links table.
        story.append(Paragraph("Identity Links", styles["Heading1"]))
        link_rows = [["Account A", "Account B", "Score", "Tier", "Signals"]]
        for link in json_package.get("identity_links", []):
            link_rows.append([
                str(link.get("account_a", ""))[:40],
                str(link.get("account_b", ""))[:40],
                str(link.get("confidence_score", "")),
                str(link.get("confidence_tier", "")),
                str(link.get("signal_count", "")),
            ])
        link_table = Table(link_rows, colWidths=[4.5 * cm, 4.5 * cm, 2 * cm, 2 * cm, 2 * cm])
        link_table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightblue),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
        ]))
        story.append(link_table)
        story.append(Spacer(1, 0.5 * cm))

        # Preservation evidence table.
        story.append(Paragraph("Preservation Evidence", styles["Heading1"]))
        pres_rows = [["Platform", "SHA-256", "Wayback"]]
        for item in json_package.get("preservation_references", []):
            pres_rows.append([
                str(item.get("source_platform", ""))[:20],
                str(item.get("snapshot_hash", ""))[:32],
                "yes" if item.get("wayback_ref") else "no",
            ])
        pres_table = Table(pres_rows, colWidths=[4 * cm, 9 * cm, 2 * cm])
        pres_table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("FONTSIZE", (0, 0), (-1, -1), 6),
        ]))
        story.append(pres_table)
        story.append(PageBreak())

        # Appendix — audit log.
        story.append(Paragraph("Appendix — Audit Log", styles["Heading1"]))
        for entry in json_package.get("audit_log", []):
            story.append(Paragraph(
                f"{entry.get('created_at', '')} — {entry.get('event_type', '')} "
                f"by {entry.get('actor_id', '')}",
                styles["Normal"],
            ))

        doc.build(story)
        return buffer.getvalue()

    def sign_bundle(self, json_bytes: bytes, pdf_bytes: bytes) -> str:
        """SHA-256 over the concatenation of the JSON and PDF bytes."""
        return hashlib.sha256(json_bytes + pdf_bytes).hexdigest()

    def save_outputs(self, case_id: UUID, json_bytes: bytes, pdf_bytes: bytes, hash_str: str) -> dict:
        """Store the three outputs to MinIO and a local case directory."""
        prefix = f"cases/{case_id}/reports"
        minio_paths = {
            "json": f"{prefix}/{case_id}_report.json",
            "pdf": f"{prefix}/{case_id}_report.pdf",
            "sha256": f"{prefix}/{case_id}_bundle.sha256",
        }
        minio_client.put_bytes(minio_paths["json"], json_bytes, "application/json")
        minio_client.put_bytes(minio_paths["pdf"], pdf_bytes, "application/pdf")
        minio_client.put_bytes(minio_paths["sha256"], hash_str.encode("utf-8"), "text/plain")

        local_dir = os.path.join(get_settings().cases_dir, str(case_id))
        os.makedirs(local_dir, exist_ok=True)
        with open(os.path.join(local_dir, f"{case_id}_report.json"), "wb") as handle:
            handle.write(json_bytes)
        with open(os.path.join(local_dir, f"{case_id}_report.pdf"), "wb") as handle:
            handle.write(pdf_bytes)
        with open(os.path.join(local_dir, f"{case_id}_bundle.sha256"), "w", encoding="utf-8") as handle:
            handle.write(hash_str)

        return {"minio": minio_paths, "local_dir": local_dir, "bundle_sha256": hash_str}
