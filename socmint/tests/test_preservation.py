"""Tests for the Preservation Service (MODULE 4) — never drops evidence."""
from __future__ import annotations

from uuid import uuid4

from api.services.preservation import PreservationService


def test_preserve_returns_refs_even_when_all_steps_fail(monkeypatch):
    service = PreservationService()

    # Force every external step to fail; preserve() must still return a dict.
    monkeypatch.setattr(service, "_fetch_html", lambda url: (_ for _ in ()).throw(RuntimeError("no net")))
    monkeypatch.setattr(service, "_screenshot", lambda url, base: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(service, "_wayback_save", lambda url: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(service, "_pull_prior_snapshot", lambda url, base: (_ for _ in ()).throw(RuntimeError("x")))

    result = service.preserve("https://example.com/alice", uuid4(), uuid4())

    assert set(result.keys()) >= {"snapshot_ref", "snapshot_hash", "wayback_ref", "preserved_at"}
    assert result["snapshot_hash"] is None
    assert result["preserved_at"]  # always populated


def test_preserve_hashes_fetched_html(monkeypatch):
    service = PreservationService()
    monkeypatch.setattr(service, "_fetch_html", lambda url: b"<html>hi</html>")
    monkeypatch.setattr("api.db.minio_client.put_bytes", lambda *a, **k: None)
    monkeypatch.setattr(service, "_screenshot", lambda url, base: None)
    monkeypatch.setattr(service, "_wayback_save", lambda url: "https://web.archive.org/x")
    monkeypatch.setattr(service, "_pull_prior_snapshot", lambda url, base: None)

    result = service.preserve("https://example.com/alice", uuid4(), uuid4())

    assert result["snapshot_hash"] is not None
    assert len(result["snapshot_hash"]) == 64  # SHA-256 hex
    assert result["wayback_ref"] == "https://web.archive.org/x"
