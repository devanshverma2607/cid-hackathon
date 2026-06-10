"""Shared pytest fixtures and EvidenceUnit builders (pure DTO inputs)."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from api.models.evidence import EvidenceUnit


@pytest.fixture
def make_unit():
    """Factory for EvidenceUnit DTOs with sensible defaults."""

    def _build(**overrides) -> EvidenceUnit:
        defaults = dict(
            case_id=uuid4(),
            run_id=uuid4(),
            tool_name="sherlock",
            tool_version="1.0",
            tool_tier=1,
            source_platform="github",
            source_tier=2,
            seed_type="username",
            seed_value="alice",
            result_type="account_found",
            result_value="https://github.com/alice",
            analyst_id="analyst-1",
            timestamp_collected=datetime.now(timezone.utc),
        )
        defaults.update(overrides)
        return EvidenceUnit(**defaults)

    return _build
