"""Tests for the Report Generator (MODULE 8) — bundle signing is deterministic."""
from __future__ import annotations

import hashlib

from api.services.report_generator import ReportGenerator


def test_sign_bundle_is_sha256_of_concatenation():
    gen = ReportGenerator()
    json_bytes = b'{"case": "demo"}'
    pdf_bytes = b"%PDF-1.4 fake"
    expected = hashlib.sha256(json_bytes + pdf_bytes).hexdigest()
    assert gen.sign_bundle(json_bytes, pdf_bytes) == expected


def test_sign_bundle_is_deterministic():
    gen = ReportGenerator()
    a = gen.sign_bundle(b"x", b"y")
    b = gen.sign_bundle(b"x", b"y")
    assert a == b


def test_sign_bundle_changes_with_content():
    gen = ReportGenerator()
    a = gen.sign_bundle(b"x", b"y")
    b = gen.sign_bundle(b"x", b"z")
    assert a != b
