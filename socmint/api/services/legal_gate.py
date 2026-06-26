"""MODULE 1 — Legal Gate & Seed Manager.

Validates the mandatory authorisation fields before any collection begins,
normalises the seed, and issues case/run identifiers. See MODULE 1 (Section 5)
of SOCMINT_PLAN_v2_0.txt. The legal gate is a hard control: the pipeline must
not start until validate() passes.
"""
from __future__ import annotations

import os
import re
import uuid
from uuid import UUID
from typing import TYPE_CHECKING, Optional

from api.models.case import CaseCreate

if TYPE_CHECKING:
    from api.models.user import UserOut

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+\.[A-Za-z]{2,}(?:[:/?#].*)?$", re.I)

# URL path segments that are routing prefixes, not the account handle itself
# (e.g. linkedin.com/in/<handle>, facebook.com/people/<name>/<id>).
_URL_SKIP_SEGMENTS = {
    "in", "pub", "profile", "people", "user", "users", "u", "@", "channel", "c",
}

# Default region for parsing phone numbers entered without an international
# dialling prefix. The system is primarily operated in India, so a bare
# "9876543210" or "09876543210" is interpreted as an Indian (+91) number.
# Override with the DEFAULT_PHONE_REGION env var (ISO 3166-1 alpha-2) if needed.
DEFAULT_PHONE_REGION = os.environ.get("DEFAULT_PHONE_REGION", "IN").upper()

VALID_TARGET_CATEGORIES = {"cybercrime", "fraud", "harassment", "research"}
VALID_SEED_TYPES = {"username", "email", "phone", "profile_url"}

# The 10 mandatory fields from MODULE 1.
MANDATORY_FIELDS = (
    "authority_id",
    "agency_id",
    "analyst_id",
    "supervisor_approval",
    "purpose_statement",
    "target_category",
    "jurisdiction",
    "retention_period",
    "seed_type",
    "seed_value",
)


class LegalGate:
    """Enforces authorisation, normalises the seed, and issues identifiers."""

    def validate(
        self,
        case_data: CaseCreate,
        *,
        current_user: Optional[UserOut] = None,
    ) -> tuple[bool, list[str]]:
        """Return (True, []) if all checks pass, else (False, [bad fields]).
        When *current_user* is supplied (the authenticated analyst), the gate
        enforces **dual-control**: ``supervisor_approval`` requires that the
        creating user holds the ``supervisor`` or ``admin`` role. An analyst
        cannot self-approve.
        """
        errors: list[str] = []
        data = case_data.model_dump()

        # Non-empty checks for all 10 mandatory fields.
        for field in MANDATORY_FIELDS:
            value = data.get(field)
            if value is None:
                errors.append(field)
                continue
            if isinstance(value, str) and not value.strip():
                errors.append(field)

        # supervisor_approval must be True.
        if data.get("supervisor_approval") is not True:
            errors.append("supervisor_approval")

        # --- Dual-control: the creating user must be supervisor/admin ------
        if current_user is not None and data.get("supervisor_approval") is True:
            if current_user.role not in ("supervisor", "admin"):
                errors.append("supervisor_approval")

        # purpose_statement must be at least 20 characters.
        purpose = data.get("purpose_statement") or ""
        if len(purpose.strip()) < 20:
            errors.append("purpose_statement")

        # target_category must be one of the four valid values.
        if data.get("target_category") not in VALID_TARGET_CATEGORIES:
            errors.append("target_category")

        # retention_period must be a positive integer.
        retention = data.get("retention_period")
        if not isinstance(retention, int) or retention < 1:
            errors.append("retention_period")

        # seed_type must be valid, and seed_value must pass format validation.
        seed_type = data.get("seed_type")
        seed_value = data.get("seed_value") or ""
        if seed_type not in VALID_SEED_TYPES:
            errors.append("seed_type")
        elif not self._validate_seed_format(seed_type, seed_value):
            errors.append("seed_value")

        # De-duplicate while preserving order.
        ordered_unique = list(dict.fromkeys(errors))
        return (len(ordered_unique) == 0, ordered_unique)

    def _validate_seed_format(self, seed_type: str, seed_value: str) -> bool:
        """Format-check a seed for its declared type."""
        value = (seed_value or "").strip()
        if not value:
            return False
        if seed_type == "email":
            return bool(EMAIL_RE.match(value.lower()))
        if seed_type == "phone":
            digits = re.sub(r"\D", "", value)
            return len(digits) >= 7
        if seed_type == "username":
            return len(value.lstrip("@").strip()) >= 1
        if seed_type == "profile_url":
            return bool(URL_RE.match(value)) and self._extract_handle_from_url(value) != ""
        return False

    def validate_seed(self, seed_type: str, seed_value: str) -> bool:
        """Public single-seed validation (type known + format valid).

        Used for each additional identifier supplied alongside the primary seed.
        """
        return seed_type in VALID_SEED_TYPES and self._validate_seed_format(
            seed_type, seed_value
        )

    @staticmethod
    def _extract_handle_from_url(url: str) -> str:
        """Pull the account handle out of a profile URL.

        ``https://github.com/torvalds`` -> ``torvalds``;
        ``https://linkedin.com/in/jane-doe`` -> ``jane-doe``;
        ``https://t.me/durov`` -> ``durov``. Returns ``""`` when the URL has no
        usable handle (bare domain).
        """
        cleaned = re.sub(r"^https?://", "", (url or "").strip(), flags=re.I)
        cleaned = cleaned.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        parts = [p for p in cleaned.split("/")[1:] if p]  # drop the host
        for segment in reversed(parts):
            handle = segment.lstrip("@").strip()
            if handle and handle.lower() not in _URL_SKIP_SEGMENTS and not handle.isdigit():
                return handle
        return ""

    def resolve_dispatch_seed(self, seed_type: str, seed_value: str) -> tuple[str, str]:
        """Map a stored seed to the (type, value) the pipeline should run.

        Username/email/phone pass through unchanged. A ``profile_url`` is
        resolved to a ``username`` sweep on the handle extracted from the URL so
        the existing Tier 1/2 chains can act on it.
        """
        if seed_type == "profile_url":
            handle = self._extract_handle_from_url(seed_value)
            return ("username", handle.lower()) if handle else ("username", seed_value)
        return seed_type, seed_value

    def normalise_seed(self, seed_type: str, seed_value: str) -> str:
        """Apply MODULE 1 normalisation rules per seed type."""
        value = (seed_value or "").strip()

        if seed_type == "username":
            return value.lstrip("@").strip().lower()

        if seed_type == "profile_url":
            # Preserve the full URL (lower-cased host/path) for provenance; the
            # dispatch handle is derived separately via resolve_dispatch_seed().
            return value.rstrip("/")

        if seed_type == "email":
            normalised = value.strip().lower()
            if not EMAIL_RE.match(normalised):
                raise ValueError(f"invalid email format: {seed_value}")
            return normalised

        if seed_type == "phone":
            try:
                import phonenumbers

                # Try E.164 input first (e.g. "+919876543210"); if that yields no
                # valid number, re-parse assuming the default region (India) so
                # locally-entered numbers without a +91 prefix still normalise.
                parsed = phonenumbers.parse(value, None)
                if not phonenumbers.is_valid_number(parsed):
                    parsed = phonenumbers.parse(value, DEFAULT_PHONE_REGION)
                return phonenumbers.format_number(
                    parsed, phonenumbers.PhoneNumberFormat.E164
                )
            except Exception:
                # Final fallback: parse digits against the default region.
                try:
                    import phonenumbers

                    digits = re.sub(r"\D", "", value)
                    parsed = phonenumbers.parse(digits, DEFAULT_PHONE_REGION)
                    return phonenumbers.format_number(
                        parsed, phonenumbers.PhoneNumberFormat.E164
                    )
                except Exception:
                    digits = re.sub(r"\D", "", value)
                    return f"+{digits}" if digits else value

        return value

    def issue_case_id(self) -> UUID:
        """Issue a fresh case identifier."""
        return uuid.uuid4()

    def issue_run_id(self) -> UUID:
        """Issue a fresh run identifier."""
        return uuid.uuid4()
