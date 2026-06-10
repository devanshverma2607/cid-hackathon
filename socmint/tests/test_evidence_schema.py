"""Tests for the EvidenceUnit schema (MODULE 0)."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from api.models.evidence import EvidenceUnit


def test_auto_generated_provenance(make_unit):
    unit = make_unit()
    assert isinstance(unit.evidence_id, UUID)
    assert isinstance(unit.timestamp_collected, datetime)
    assert unit.timestamp_collected.tzinfo is not None


def test_invalid_result_type_rejected(make_unit):
    with pytest.raises(ValidationError):
        make_unit(result_type="totally_invalid")


def test_invalid_tool_tier_rejected(make_unit):
    with pytest.raises(ValidationError):
        make_unit(tool_tier=9)


def test_optional_fields_default_none(make_unit):
    unit = make_unit()
    assert unit.snapshot_ref is None
    assert unit.bio_embedding is None
    assert unit.platform_enrichment is None


def test_validate_assignment_enforced(make_unit):
    unit = make_unit()
    with pytest.raises(ValidationError):
        unit.result_type = "nope"


def test_bio_embedding_accepts_float_list(make_unit):
    unit = make_unit(bio_embedding=[0.1, 0.2, 0.3])
    assert len(unit.bio_embedding) == 3
