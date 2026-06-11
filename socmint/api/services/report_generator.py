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


# Self-contained styling + behaviour for the interactive HTML report. Kept as
# plain strings (no external CDN) so the report opens offline in any browser —
# a hard requirement for a court-ready forensic artifact.
_HTML_CSS = """
:root{--hi:#c0392b;--med:#e67e22;--low:#f1c40f;--ok:#27ae60;--ink:#1a1a2e;
--muted:#666;--line:#dcdce4;--bg:#f5f6fa;--card:#fff;--accent:#2c3e50}
*{box-sizing:border-box}
body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:0;
background:var(--bg);color:var(--ink);line-height:1.5}
header{background:var(--accent);color:#fff;padding:24px 32px}
header h1{margin:0 0 4px;font-size:22px}
header .meta{font-size:13px;opacity:.85}
.wrap{max-width:1200px;margin:0 auto;padding:8px 32px 32px}
.cards{display:flex;flex-wrap:wrap;gap:16px;margin:16px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:14px 18px;flex:1 1 150px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
.card .n{font-size:26px;font-weight:700}
.card .l{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;
margin:16px 0;overflow:hidden}
section>h2{margin:0;padding:14px 20px;font-size:15px;background:#eef0f6;cursor:pointer;
display:flex;justify-content:space-between;align-items:center}
section>h2:after{content:"\\25BC";font-size:10px;color:var(--muted)}
section.collapsed>h2:after{content:"\\25B6"}
section.collapsed>.body{display:none}
.body{padding:16px 20px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);
vertical-align:top;word-break:break-word}
th{background:#fafbfe;cursor:pointer;user-select:none}
th:hover{background:#eef}
tr:hover td{background:#fafbff}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;
font-weight:700;color:#fff}
.b-HIGH{background:var(--hi)}.b-MEDIUM{background:var(--med)}
.b-LOW{background:var(--low);color:#333}.b-DISCARD{background:#999}
.b-CONFIRMED{background:var(--ok)}.b-REJECTED{background:#999}.b-PENDING{background:#888}
.controls{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
#q{flex:1 1 280px;padding:9px 12px;border:1px solid var(--line);border-radius:8px;font-size:14px}
.fbtn{padding:6px 12px;border:1px solid var(--line);background:#fff;border-radius:8px;
cursor:pointer;font-size:12px}
.fbtn.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.gauge{margin:10px 0}
.gauge .lab{font-size:12px;color:var(--muted);display:flex;justify-content:space-between;margin-bottom:3px}
.gauge .track{height:14px;background:#eceef5;border-radius:8px;overflow:hidden}
.gauge .fill{height:100%;border-radius:8px}
.narr{background:#f8f9fc;border-left:4px solid var(--accent);padding:12px 16px;
border-radius:0 8px 8px 0;margin:8px 0;font-size:14px;white-space:pre-wrap}
.ai-tag{display:inline-block;font-size:10px;font-weight:700;color:#fff;background:#6c5ce7;
padding:1px 6px;border-radius:6px;margin-left:6px;vertical-align:middle}
.muted{color:var(--muted)}
.kv{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;font-size:13px;margin:0}
.kv dt{color:var(--muted)}.kv dd{margin:0}
.bars{display:flex;gap:24px;align-items:flex-end;height:130px;padding:8px 4px}
.bar{display:flex;flex-direction:column;align-items:center;gap:6px;font-size:12px;justify-content:flex-end}
.bar .col{width:54px;border-radius:6px 6px 0 0;min-height:2px}
.disclaimer{background:#fff8e1;border:1px solid #ffe082;border-radius:8px;padding:10px 14px;
font-size:12px;color:#7a5c00;margin:12px 0}
footer{max-width:1200px;margin:0 auto;padding:16px 32px;color:var(--muted);font-size:12px}
.hidden{display:none!important}
"""

_HTML_JS = """
(function(){
 document.querySelectorAll('section>h2').forEach(function(h){
   h.addEventListener('click',function(){h.parentElement.classList.toggle('collapsed');});
 });
 function updateRow(tr){tr.classList.toggle('hidden',!!(tr.dataset.qhide||tr.dataset.thide));}
 var q=document.getElementById('q');
 if(q){q.addEventListener('input',function(){
   var t=(q.value||'').toLowerCase();
   document.querySelectorAll('table.searchable tbody tr').forEach(function(tr){
     tr.dataset.qhide=(!t||tr.textContent.toLowerCase().indexOf(t)>=0)?'':'1';updateRow(tr);
   });
 });}
 var tier='ALL';
 document.querySelectorAll('.fbtn[data-tier]').forEach(function(b){
   b.addEventListener('click',function(){
     document.querySelectorAll('.fbtn[data-tier]').forEach(function(x){x.classList.remove('active');});
     b.classList.add('active');tier=b.dataset.tier;
     document.querySelectorAll('#links-table tbody tr').forEach(function(tr){
       tr.dataset.thide=(tier==='ALL'||tr.dataset.tier===tier)?'':'1';updateRow(tr);
     });
   });
 });
 document.querySelectorAll('table.sortable th').forEach(function(th){
   th.addEventListener('click',function(){
     var tb=th.closest('table'),body=tb.querySelector('tbody');
     var rows=[].slice.call(body.querySelectorAll('tr'));
     var ci=Array.prototype.indexOf.call(th.parentElement.children,th);
     var asc=th.dataset.asc!=='1';th.dataset.asc=asc?'1':'0';
     rows.sort(function(a,b){
       var x=(a.children[ci]||{}).textContent||'',y=(b.children[ci]||{}).textContent||'';
       var nx=parseFloat(x),ny=parseFloat(y);
       if(!isNaN(nx)&&!isNaN(ny))return asc?nx-ny:ny-nx;
       return asc?x.localeCompare(y):y.localeCompare(x);
     });
     rows.forEach(function(r){body.appendChild(r);});
   });
 });
})();
"""


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

        # The signed report is an explicit, slower action, so it includes the
        # optional local-LLM narratives (include_ai=True) — unlike the live
        # dashboard endpoints, which keep them off to render instantly.
        intelligence = InsightEngine().assess(
            clean_evidence, clean_links, clean_case, include_ai=True
        )

        try:
            persona = PersonaResolver().resolve(case_id, session)
        except Exception:
            persona = None
        subject_dossier = ProfileEngine().build(
            clean_evidence, clean_links, clean_case, persona, include_ai=True
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
        for c in (attrs.get("contact_numbers") or [])[:6]:
            label = "Contact number" + (" (masked)" if c.get("obfuscated") else "")
            via = ", ".join((c.get("sources") or [])[:3])
            val = str(c.get("value", "")) + (f"  ·  via {via}" if via else "")
            attr_rows.append([label, Paragraph(val, styles["BodyText"]),
                              str(c.get("confidence", "")).upper()])
        for c in (attrs.get("candidate_emails") or [])[:6]:
            plats = ", ".join((c.get("platforms") or [])[:5])
            val = str(c.get("value", "")) + (f"  ·  registered on {plats}" if plats else "")
            attr_rows.append(["Candidate email (unconfirmed)",
                              Paragraph(val, styles["BodyText"]), "LOW"])

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

    # ----------------------------------------------------- interactive HTML
    @staticmethod
    def _html_escape(value) -> str:
        """Escape any value for safe HTML insertion (prevents stored XSS from
        attacker-controlled OSINT strings rendered in a browser)."""
        import html as _html

        return "" if value is None else _html.escape(str(value), quote=True)

    def _html_table(self, table_id, headers, rows, row_attrs=None,
                    classes="searchable sortable") -> str:
        """Render a table; cells are escaped unless passed as ('raw', html)."""
        esc = self._html_escape
        head = "".join(f"<th>{esc(h)}</th>" for h in headers)
        out = []
        for i, row in enumerate(rows):
            attrs = ""
            if row_attrs and i < len(row_attrs) and row_attrs[i]:
                attrs = "".join(f' {k}="{esc(v)}"' for k, v in row_attrs[i].items())
            cells = []
            for cell in row:
                if isinstance(cell, tuple) and len(cell) == 2 and cell[0] == "raw":
                    cells.append(f"<td>{cell[1]}</td>")
                else:
                    cells.append(f"<td>{esc(cell)}</td>")
            out.append(f"<tr{attrs}>{''.join(cells)}</tr>")
        empty = "" if rows else '<tr><td class="muted">No records.</td></tr>'
        return (
            f'<table id="{esc(table_id)}" class="{esc(classes)}">'
            f"<thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(out)}{empty}</tbody></table>"
        )

    def _html_gauge(self, label, score, band) -> str:
        esc = self._html_escape
        try:
            pct = max(0.0, min(100.0, float(score or 0)))
        except (TypeError, ValueError):
            pct = 0.0
        color = {
            "CRITICAL": "#c0392b", "HIGH": "#c0392b", "ELEVATED": "#e67e22",
            "MEDIUM": "#e67e22", "MODERATE": "#e67e22", "LOW": "#27ae60",
            "MINIMAL": "#27ae60",
        }.get(str(band).upper(), "#2c3e50")
        return (
            f'<div class="gauge"><div class="lab"><span>{esc(label)}</span>'
            f'<span><b>{esc(band)}</b> {pct:.0f}/100</span></div>'
            f'<div class="track"><div class="fill" style="width:{pct:.0f}%;'
            f'background:{color}"></div></div></div>'
        )

    def generate_html_report(self, case_id: UUID, json_package: dict) -> bytes:
        """Render a self-contained, searchable, interactive HTML report.

        A human-friendly *view* of the same data captured in the signed JSON
        package; every attacker-controllable string is HTML-escaped. No external
        assets, so it opens offline in any browser.
        """
        esc = self._html_escape
        case = json_package.get("case") or {}
        intel = json_package.get("intelligence_assessment") or {}
        dossier = json_package.get("subject_dossier") or {}
        summary = json_package.get("summary") or {}
        links = json_package.get("identity_links") or []
        evidence = json_package.get("evidence_units") or []
        preservation = json_package.get("preservation_references") or []
        audit = json_package.get("audit_log") or []
        risk = intel.get("risk") or {}
        expo = intel.get("exposure_score") or {}
        generated = json_package.get("generated_at") or ""

        cards = [
            ("Evidence units", summary.get("total_evidence_units", len(evidence))),
            ("Identity links", summary.get("total_identity_links", len(links))),
            ("Confirmed", summary.get("confirmed_links", 0)),
            ("HIGH", summary.get("high", 0)),
            ("MEDIUM", summary.get("medium", 0)),
            ("LOW", summary.get("low", 0)),
        ]
        cards_html = "".join(
            f'<div class="card"><div class="n">{esc(v)}</div>'
            f'<div class="l">{esc(l)}</div></div>'
            for l, v in cards
        )

        hi, med, low = summary.get("high", 0), summary.get("medium", 0), summary.get("low", 0)
        mx = max(hi, med, low, 1)

        def _bar(label, n, color):
            h = int(8 + (n / mx) * 104)
            return (f'<div class="bar"><div>{esc(n)}</div>'
                    f'<div class="col" style="height:{h}px;background:{color}"></div>'
                    f'<div>{esc(label)}</div></div>')

        bars = ('<div class="bars">' + _bar("HIGH", hi, "#c0392b")
                + _bar("MEDIUM", med, "#e67e22") + _bar("LOW", low, "#f1c40f") + "</div>")
        gauges = (self._html_gauge("Threat risk", risk.get("score"), risk.get("band"))
                  + self._html_gauge("Footprint exposure", expo.get("score"), expo.get("band")))

        def _narr(title, deterministic, ai):
            block = f"<h3 style='margin:6px 0 4px;font-size:14px'>{esc(title)}</h3>"
            if ai:
                block += f'<div class="narr">{esc(ai)}<span class="ai-tag">AI DRAFT</span></div>'
            if deterministic:
                block += f'<div class="narr muted">{esc(deterministic)}</div>'
            if not ai and not deterministic:
                block += '<p class="muted">No narrative available.</p>'
            return block

        narratives = (_narr("Intelligence assessment", intel.get("narrative"), intel.get("ai_narrative"))
                      + _narr("Subject dossier", dossier.get("summary"), dossier.get("ai_summary")))

        link_rows, link_attrs = [], []
        for l in links:
            tier = str(l.get("confidence_tier") or "")
            decision = str(l.get("analyst_decision") or "PENDING")
            sigs = ", ".join(
                k for k in (l.get("signal_breakdown") or {}) if not str(k).startswith("_")
            )
            link_rows.append([
                ("raw", f'<span class="badge b-{esc(tier)}">{esc(tier)}</span>'),
                f"{l.get('account_a','')} ({l.get('platform_a','')})",
                f"{l.get('account_b','')} ({l.get('platform_b','')})",
                l.get("confidence_score", ""),
                l.get("signal_count", ""),
                sigs,
                ("raw", f'<span class="badge b-{esc(decision)}">{esc(decision)}</span>'),
            ])
            link_attrs.append({"data-tier": tier})
        links_table = self._html_table(
            "links-table",
            ["Tier", "Account A", "Account B", "Score", "Signals", "Signal breakdown", "Decision"],
            link_rows, link_attrs,
        )

        accounts = (intel.get("subject_profile") or {}).get("confirmed_accounts") or []
        acct_rows = []
        for a in accounts:
            if isinstance(a, dict):
                acct_rows.append([
                    a.get("platform", ""),
                    a.get("handle") or a.get("username") or a.get("value", ""),
                    a.get("url", ""),
                ])
            else:
                acct_rows.append([a, "", ""])
        accounts_table = self._html_table("accounts-table", ["Platform", "Handle", "URL"], acct_rows)

        ev_rows = [[
            e.get("source_platform", ""), e.get("tool_name", ""), e.get("result_type", ""),
            e.get("result_value", ""), str(e.get("timestamp_collected", ""))[:19],
        ] for e in evidence]
        evidence_table = self._html_table(
            "evidence-table", ["Platform", "Tool", "Type", "Value", "Collected"], ev_rows)

        pres_rows = [[
            p.get("source_platform", ""), p.get("result_value", ""),
            p.get("snapshot_hash", ""), p.get("wayback_ref", ""),
        ] for p in preservation]
        pres_table = self._html_table(
            "pres-table", ["Platform", "Value", "Snapshot SHA-256", "Wayback"], pres_rows)

        audit_rows = [[
            str(x.get("created_at", ""))[:19], x.get("event_type", ""), x.get("actor_id", ""),
        ] for x in audit]
        audit_table = self._html_table("audit-table", ["Timestamp", "Event", "Actor"], audit_rows)

        def _section(title, inner, collapsed=False):
            cls = " class='collapsed'" if collapsed else ""
            return f"<section{cls}><h2>{title}</h2><div class='body'>{inner}</div></section>"

        controls = (
            '<div class="controls"><input id="q" placeholder="Search all tables..." />'
            '<button class="fbtn active" data-tier="ALL">All</button>'
            '<button class="fbtn" data-tier="HIGH">High</button>'
            '<button class="fbtn" data-tier="MEDIUM">Medium</button>'
            '<button class="fbtn" data-tier="LOW">Low</button></div>'
        )
        case_kv = "".join(
            f"<dt>{esc(k)}</dt><dd>{esc(case.get(k, ''))}</dd>"
            for k in ("case_id", "authority_id", "agency_id", "analyst_id", "target_category",
                      "jurisdiction", "seed_type", "seed_value", "created_at")
            if case.get(k) is not None
        )

        body = (
            '<div class="wrap">'
            '<div class="disclaimer">Lawful OSINT assessment for an authorised investigation. '
            'All inferences are investigative leads requiring independent verification — not '
            'determinations of identity or guilt. The authoritative, integrity-signed artifacts '
            'are the JSON package and PDF (bundle SHA-256).</div>'
            f'<div class="cards">{cards_html}</div>'
            + _section("Case metadata", f'<dl class="kv">{case_kv}</dl>')
            + _section("Risk &amp; exposure", gauges + bars)
            + _section("Narratives", narratives)
            + _section("Identity links", controls + links_table)
            + _section("Confirmed accounts", accounts_table)
            + _section("Evidence units", evidence_table, collapsed=True)
            + _section("Preservation references", pres_table, collapsed=True)
            + _section("Audit log", audit_table, collapsed=True)
            + "</div>"
        )

        title = f"SOCMINT Report — {esc(case.get('seed_value') or case_id)}"
        html_doc = (
            "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><style>{_HTML_CSS}</style></head><body>"
            "<header><h1>SOCMINT Intelligence Report</h1>"
            f"<div class='meta'>Case {esc(case.get('case_id') or case_id)} &nbsp;·&nbsp; "
            f"Subject seed: {esc(case.get('seed_value') or '—')} &nbsp;·&nbsp; "
            f"Generated {esc(generated)}</div></header>"
            f"{body}"
            "<footer>Generated by SOCMINT · Interactive view of the signed evidence package · "
            "Search, sort (click headers) and filter are client-side only.</footer>"
            f"<script>{_HTML_JS}</script></body></html>"
        )
        return html_doc.encode("utf-8")

    def sign_bundle(self, json_bytes: bytes, pdf_bytes: bytes) -> str:
        """SHA-256 over the concatenation of the JSON and PDF bytes."""
        return hashlib.sha256(json_bytes + pdf_bytes).hexdigest()

    def save_outputs(self, case_id: UUID, json_bytes: bytes, pdf_bytes: bytes, hash_str: str,
                     html_bytes: bytes | None = None) -> dict:
        """Store the outputs to MinIO and a local case directory."""
        prefix = f"cases/{case_id}/reports"
        minio_paths = {
            "json": f"{prefix}/{case_id}_report.json",
            "pdf": f"{prefix}/{case_id}_report.pdf",
            "sha256": f"{prefix}/{case_id}_bundle.sha256",
        }
        minio_client.put_bytes(minio_paths["json"], json_bytes, "application/json")
        minio_client.put_bytes(minio_paths["pdf"], pdf_bytes, "application/pdf")
        minio_client.put_bytes(minio_paths["sha256"], hash_str.encode("utf-8"), "text/plain")
        if html_bytes is not None:
            minio_paths["html"] = f"{prefix}/{case_id}_report.html"
            minio_client.put_bytes(minio_paths["html"], html_bytes, "text/html; charset=utf-8")

        local_dir = os.path.join(get_settings().cases_dir, str(case_id))
        os.makedirs(local_dir, exist_ok=True)
        with open(os.path.join(local_dir, f"{case_id}_report.json"), "wb") as handle:
            handle.write(json_bytes)
        with open(os.path.join(local_dir, f"{case_id}_report.pdf"), "wb") as handle:
            handle.write(pdf_bytes)
        with open(os.path.join(local_dir, f"{case_id}_bundle.sha256"), "w", encoding="utf-8") as handle:
            handle.write(hash_str)
        if html_bytes is not None:
            with open(os.path.join(local_dir, f"{case_id}_report.html"), "wb") as handle:
                handle.write(html_bytes)

        return {"minio": minio_paths, "local_dir": local_dir, "bundle_sha256": hash_str}
