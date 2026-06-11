"""/api/v1/reports — generate and download the signed evidence bundle."""
from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.db import minio_client
from api.db.postgres import get_db
from api.services.report_generator import ReportGenerator

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])


@router.post("/generate/{case_id}")
def generate_report(case_id: UUID, session: Session = Depends(get_db)) -> dict:
    """Build the JSON package + PDF, sign the bundle, and store all outputs."""
    generator = ReportGenerator()
    json_package = generator.generate_json_package(case_id, session)
    json_bytes = json.dumps(json_package, default=str, indent=2).encode("utf-8")
    pdf_bytes = generator.generate_pdf_report(case_id, json_package)
    html_bytes = generator.generate_html_report(case_id, json_package)
    bundle_hash = generator.sign_bundle(json_bytes, pdf_bytes)
    outputs = generator.save_outputs(case_id, json_bytes, pdf_bytes, bundle_hash, html_bytes)
    return {"case_id": str(case_id), "bundle_sha256": bundle_hash, "outputs": outputs}


def _download(case_id: UUID, suffix: str, media_type: str) -> Response:
    object_path = f"cases/{case_id}/reports/{case_id}_{suffix}"
    try:
        data = minio_client.get_object_bytes(object_path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"report not found: {exc}") from exc
    return Response(content=data, media_type=media_type)


@router.get("/download/{case_id}/json")
def download_json(case_id: UUID) -> Response:
    """Download the JSON evidence package."""
    return _download(case_id, "report.json", "application/json")


@router.get("/download/{case_id}/pdf")
def download_pdf(case_id: UUID) -> Response:
    """Download the PDF report."""
    return _download(case_id, "report.pdf", "application/pdf")


@router.get("/download/{case_id}/html")
def download_html(case_id: UUID) -> Response:
    """Download the interactive HTML report."""
    return _download(case_id, "report.html", "text/html; charset=utf-8")


@router.get("/download/{case_id}/sha256")
def download_sha256(case_id: UUID) -> Response:
    """Download the bundle SHA-256 manifest."""
    return _download(case_id, "bundle.sha256", "text/plain")


@router.get("/status/{case_id}")
def report_status(case_id: UUID, session: Session = Depends(get_db)) -> dict:
    """Report readiness summary for a case."""
    evidence_count = session.execute(
        text("SELECT COUNT(*) FROM evidence_units WHERE case_id = :cid"),
        {"cid": str(case_id)},
    ).scalar_one()
    link_count = session.execute(
        text("SELECT COUNT(*) FROM identity_links WHERE case_id = :cid"),
        {"cid": str(case_id)},
    ).scalar_one()
    return {
        "case_id": str(case_id),
        "evidence_units": int(evidence_count),
        "identity_links": int(link_count),
        "ready": int(evidence_count) > 0,
    }
