"""Tests for the Legal Gate (MODULE 1)."""
from __future__ import annotations

import pytest

from api.models.case import CaseCreate
from api.services.legal_gate import LegalGate


def _valid_payload(**overrides) -> CaseCreate:
    data = dict(
        authority_id="AUTH-1",
        agency_id="AG-1",
        analyst_id="analyst-1",
        supervisor_approval=True,
        purpose_statement="Lawful investigation into coordinated fraud activity.",
        target_category="fraud",
        jurisdiction="IN",
        retention_period=90,
        seed_type="username",
        seed_value="suspect01",
    )
    data.update(overrides)
    return CaseCreate(**data)


def test_valid_case_passes():
    ok, errors = LegalGate().validate(_valid_payload())
    assert ok is True
    assert errors == []


def test_missing_supervisor_approval_rejected():
    with pytest.raises(Exception):
        # CaseCreate validator forbids supervisor_approval=False at model build.
        _valid_payload(supervisor_approval=False)


def test_short_purpose_statement_rejected():
    with pytest.raises(Exception):
        _valid_payload(purpose_statement="too short")


def test_invalid_target_category_rejected():
    # target_category is a Literal — an invalid value fails at model construction.
    with pytest.raises(Exception):
        _valid_payload(target_category="random")


def test_invalid_email_seed_rejected():
    ok, errors = LegalGate().validate(_valid_payload(seed_type="email", seed_value="not-an-email"))
    assert ok is False
    assert "seed_value" in errors


@pytest.mark.parametrize(
    "raw,expected",
    [("@Alice", "alice"), ("BOB", "bob"), ("  user.name  ", "user.name")],
)
def test_username_normalisation(raw, expected):
    assert LegalGate().normalise_seed("username", raw) == expected


def test_email_normalisation_lowercases():
    assert LegalGate().normalise_seed("email", "John.Doe@Example.COM") == "john.doe@example.com"


def test_phone_normalisation_e164_fallback():
    result = LegalGate().normalise_seed("phone", "(415) 555-0132")
    assert result.startswith("+")


def test_issue_ids_unique():
    gate = LegalGate()
    assert gate.issue_case_id() != gate.issue_run_id()
