"""MODULE 5 — Data Normaliser.

Tags tier, applies leet-speak normalisation, lowercases identifiers, applies
evidence-age decay, deduplicates, and computes bio embeddings for pgvector.
See MODULE 5 (Section 5) and Section 9.5/9.6 of SOCMINT_PLAN_v2_0.txt.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache

from api.models.evidence import EvidenceUnit

# Section 9.6 — leet-speak normalisation map (applied before username matching).
LEET_MAP = {
    "0": "o",
    "1": "i",
    "3": "e",
    "4": "a",
    "5": "s",
    "7": "t",
    "@": "a",
}

# Section 9.6 — generic username blacklist (automatic -8 penalty in correlation).
USERNAME_BLACKLIST = {
    "admin", "user", "test", "info", "help", "support", "contact", "hello",
    "noreply", "no-reply", "webmaster", "postmaster", "root", "guest",
}

# Default decay period (days) and floor used when normalising confidence_raw.
DEFAULT_DECAY_PERIOD = 365.0
DEFAULT_MIN_DECAY = 0.50


def leet_normalise(value: str) -> str:
    """Apply leet-speak substitutions and lowercase."""
    lowered = (value or "").lower()
    return "".join(LEET_MAP.get(ch, ch) for ch in lowered)


def decay_factor(evidence_age_days: int, decay_period: float, min_decay: float) -> float:
    """Evidence decay formula (Section 9.5)."""
    return max(min_decay, 1.0 - (evidence_age_days / decay_period))


class DataNormaliser:
    """Normalise and deduplicate a batch of EvidenceUnits."""

    def normalise(self, raw_units: list[EvidenceUnit]) -> list[EvidenceUnit]:
        """Apply all normalisation steps and return deduplicated units."""
        for unit in raw_units:
            # Leet-speak + lowercase for discovered account values.
            if unit.result_type == "account_found":
                unit.result_value = self._normalise_value(unit.result_value)

            # Lowercase usernames and emails in seed/result values.
            if unit.seed_type in ("username", "email"):
                unit.seed_value = (unit.seed_value or "").lower()

            # Evidence age decay applied to confidence_raw when present.
            if unit.confidence_raw is not None:
                age_days = self._age_days(unit.timestamp_collected)
                factor = decay_factor(age_days, DEFAULT_DECAY_PERIOD, DEFAULT_MIN_DECAY)
                unit.confidence_raw = unit.confidence_raw * factor

        return self._deduplicate(raw_units)

    def _normalise_value(self, value: str) -> str:
        """Leet-normalise only the handle portion of a URL or a bare handle."""
        if "://" in value:
            # Leave URLs intact but lowercase them for stable dedup.
            return value.lower()
        return leet_normalise(value)

    @staticmethod
    def _age_days(collected: datetime) -> int:
        now = datetime.now(timezone.utc)
        if collected.tzinfo is None:
            collected = collected.replace(tzinfo=timezone.utc)
        return max(0, (now - collected).days)

    @staticmethod
    def _deduplicate(units: list[EvidenceUnit]) -> list[EvidenceUnit]:
        """Dedup on (case_id, source_platform, result_value, seed_value)."""
        best: dict[tuple, EvidenceUnit] = {}
        for unit in units:
            key = (str(unit.case_id), unit.source_platform, unit.result_value, unit.seed_value)
            current = best.get(key)
            if current is None or (unit.confidence_raw or 0) > (current.confidence_raw or 0):
                best[key] = unit
        return list(best.values())

    def compute_bio_embedding(self, bio_text: str) -> list[float]:
        """Compute a 384-dim embedding for bio text via sentence-transformers."""
        model = _get_embedding_model()
        vector = model.encode(bio_text or "")
        return [float(x) for x in vector]


@lru_cache(maxsize=1)
def _get_embedding_model():
    """Lazily load and cache the sentence-transformers model."""
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer("all-MiniLM-L6-v2")
