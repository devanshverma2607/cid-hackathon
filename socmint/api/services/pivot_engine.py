"""MODULE 6b — Pivot Engine (cross-tool seed expansion / the correlation brain).

The base pipeline runs every tool against the *original* seed only. The Pivot
Engine closes the loop: it reads the evidence collected so far, extracts the NEW
identifiers other tools discovered (emails, phones, alternate usernames,
domains), and turns each into a fresh seed for the appropriate tool chain. A
Redis-backed visited set guarantees every identifier is processed at most once,
and depth / breadth / total caps keep the recursion bounded so the case can
never explode or loop forever.

This is what makes the system behave like a brain instead of a star: tool A's
output becomes tool B's input, hop after hop, until the identity cluster stops
growing or a safety cap is reached.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from api.models.evidence import EvidenceUnit
from api.services.normaliser import USERNAME_BLACKLIST, leet_normalise

logger = logging.getLogger(__name__)

# ---- bounds (env-overridable) ----------------------------------------------
PIVOT_ENABLED = os.environ.get("PIVOT_ENABLED", "true").lower() not in ("0", "false", "no")
MAX_PIVOT_DEPTH = int(os.environ.get("PIVOT_MAX_DEPTH", "2"))
MAX_SEEDS_PER_HOP = int(os.environ.get("PIVOT_MAX_SEEDS_PER_HOP", "10"))
MAX_TOTAL_SEEDS = int(os.environ.get("PIVOT_MAX_TOTAL_SEEDS", "40"))
DEFAULT_PHONE_REGION = os.environ.get("DEFAULT_PHONE_REGION", "IN").upper()

# ---- dynamic bounds --------------------------------------------------------
# Absolute safety ceilings: dynamic scaling can raise the effective caps toward
# these but NEVER beyond them, so a case can never explode regardless of
# category. All env-overridable.
PIVOT_DEPTH_CEILING = int(os.environ.get("PIVOT_DEPTH_CEILING", "4"))
PIVOT_TOTAL_CEILING = int(os.environ.get("PIVOT_TOTAL_CEILING", "120"))
PIVOT_PER_HOP_CEILING = int(os.environ.get("PIVOT_PER_HOP_CEILING", "25"))

# Per-category scaling: higher-stakes investigations justify deeper / broader
# expansion; light-touch research stays conservative.
#   category -> (depth_bonus, total_multiplier, per_hop_multiplier)
PIVOT_CATEGORY_FACTORS = {
    "cybercrime": (1, 1.6, 1.5),
    "fraud":      (1, 1.4, 1.3),
    "harassment": (0, 1.2, 1.2),
    "research":   (0, 1.0, 1.0),
}

# Pivot value ranking: hard identifiers are claimed before soft ones so the
# most valuable leads always fit inside the budget (lower = higher priority).
_SEED_PRIORITY = {"email": 0, "phone": 1, "domain": 2, "username": 3}

# Visited-set / counter TTL so finished cases don't accumulate in Redis forever.
_REDIS_TTL_SECONDS = 24 * 3600

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_DOMAIN_RE = re.compile(r"^(?:[A-Za-z0-9\-]+\.)+[A-Za-z]{2,}$")

# platform_enrichment keys that carry pivotable identifiers.
_EMAIL_KEYS = ("public_email", "email")
_EMAIL_LIST_KEYS = ("discovered_emails", "emails")
_PHONE_KEYS = ("phone", "phone_number", "public phone", "public_phone")
_USERNAME_KEYS = ("twitter_username", "telegram_username", "username")
_USERNAME_LIST_KEYS = ("discovered_usernames",)
_DOMAIN_KEYS = ("blog", "website", "url", "domain")


@dataclass(frozen=True)
class PivotSeed:
    """A newly-discovered identifier to be fed back into the pipeline."""

    seed_type: str          # "email" | "username" | "phone" | "domain"
    seed_value: str         # normalised, canonical form (used as the seed)
    via_tool: str           # tool that surfaced it
    via_platform: str       # platform the evidence came from
    source_value: str       # the evidence result_value it was pulled from

    @property
    def key(self) -> str:
        """Stable dedupe key across hops (type + canonical value)."""
        return f"{self.seed_type}:{self.seed_value}"


@dataclass(frozen=True)
class PivotBounds:
    """Effective per-case expansion caps (dynamically scaled, then clamped)."""

    max_depth: int
    max_total: int
    max_per_hop: int


# ---- normalisation helpers -------------------------------------------------
def _norm_email(value: str) -> Optional[str]:
    if not value:
        return None
    # Masked / obfuscated addresses (e.g. "r****n@gmail.com") are not real
    # pivots — never derive a seed from them.
    if any(c in value for c in ("*", "…", "•", "x…")):
        return None
    m = _EMAIL_RE.search(value)
    if not m:
        return None
    email = m.group(0).lower().strip(".")
    # Skip privacy-relay / placeholder addresses that aren't real pivots.
    if email.endswith("noreply.github.com") or email.endswith("example.com"):
        return None
    return email


def _norm_username(value: str) -> Optional[str]:
    if not value:
        return None
    handle = value.strip().lstrip("@")
    # A profile URL → take the last path segment.
    if "://" in handle or "/" in handle:
        handle = handle.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
    handle = handle.strip()
    if len(handle) < 3:
        return None
    if leet_normalise(handle) in USERNAME_BLACKLIST:
        return None
    # Reject things that are actually emails or domains.
    if "@" in handle or _DOMAIN_RE.match(handle):
        return None
    return handle


def _norm_phone(value: str) -> Optional[str]:
    if value is None:
        return None
    raw = str(value).strip()
    # Reject masked phones and anything with letters.
    if not raw or "*" in raw or any(c.isalpha() for c in raw):
        return None
    try:
        import phonenumbers

        parsed = phonenumbers.parse(raw, None)
        if not phonenumbers.is_valid_number(parsed):
            parsed = phonenumbers.parse(raw, DEFAULT_PHONE_REGION)
        if not phonenumbers.is_valid_number(parsed):
            return None
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except Exception:  # noqa: BLE001 — bad phone strings are simply not pivots
        return None


def _norm_domain(value: str) -> Optional[str]:
    if not value:
        return None
    host = value.strip().lower()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0].split("?", 1)[0].split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    if not host or not _DOMAIN_RE.match(host):
        return None
    # Drop common platform domains — pivoting them re-recons the whole site.
    if host in {
        "instagram.com", "twitter.com", "x.com", "facebook.com", "github.com",
        "t.me", "telegram.org", "linkedin.com", "tiktok.com", "youtube.com",
    }:
        return None
    return host


class PivotEngine:
    """Extracts pivotable identifiers from evidence and dedupes them."""

    # ---- extraction ---------------------------------------------------------
    def extract_pivots(self, units: Iterable[EvidenceUnit]) -> list[PivotSeed]:
        """Pull every new identifier out of a batch of evidence units."""
        pivots: list[PivotSeed] = []

        def add(seed_type: str, raw: Optional[str], unit: EvidenceUnit, src: str) -> None:
            norm = {
                "email": _norm_email,
                "username": _norm_username,
                "phone": _norm_phone,
                "domain": _norm_domain,
            }[seed_type](raw or "")
            if norm:
                pivots.append(
                    PivotSeed(
                        seed_type=seed_type,
                        seed_value=norm,
                        via_tool=unit.tool_name,
                        via_platform=unit.source_platform,
                        source_value=src or norm,
                    )
                )

        for unit in units:
            rtype = unit.result_type
            rval = unit.result_value or ""

            if rtype == "email_registered":
                add("email", rval, unit, rval)
            elif rtype == "account_found" and "@" in rval and "://" not in rval:
                add("email", rval, unit, rval)
            elif rtype in ("whatsapp_hit", "phone_intel"):
                # a phone number discovered from an email/username investigation
                # (email2whatsapp candidate, phoneinfoga-validated number) — feed
                # it back into the phone-enrichment chain.
                add("phone", rval, unit, rval)
            elif rtype == "domain_hit":
                add("domain", rval, unit, rval)

            enrichment = unit.platform_enrichment or {}
            if not isinstance(enrichment, dict):
                continue
            for key in _EMAIL_KEYS:
                if enrichment.get(key):
                    add("email", str(enrichment[key]), unit, str(enrichment[key]))
            for key in _EMAIL_LIST_KEYS:
                for item in enrichment.get(key) or []:
                    add("email", str(item), unit, str(item))
            for key in _PHONE_KEYS:
                if enrichment.get(key):
                    add("phone", str(enrichment[key]), unit, str(enrichment[key]))
            for key in _USERNAME_KEYS:
                if enrichment.get(key):
                    add("username", str(enrichment[key]), unit, str(enrichment[key]))
            for key in _USERNAME_LIST_KEYS:
                for item in enrichment.get(key) or []:
                    add("username", str(item), unit, str(item))
            for key in _DOMAIN_KEYS:
                if enrichment.get(key):
                    add("domain", str(enrichment[key]), unit, str(enrichment[key]))

        return pivots

    # ---- visited-set / bounds (Redis) --------------------------------------
    @classmethod
    def compute_bounds(cls, case: Optional[dict] = None) -> PivotBounds:
        """Scale the base caps by case category, clamped to absolute ceilings.

        A cybercrime/fraud case justifies deeper, broader expansion than a
        light-touch research lookup. Unknown categories fall back to the
        conservative baseline. The result can never exceed the safety ceilings.
        """
        category = str((case or {}).get("target_category", "")).lower()
        depth_bonus, total_mult, per_hop_mult = PIVOT_CATEGORY_FACTORS.get(
            category, (0, 1.0, 1.0)
        )
        return PivotBounds(
            max_depth=min(PIVOT_DEPTH_CEILING, MAX_PIVOT_DEPTH + depth_bonus),
            max_total=min(PIVOT_TOTAL_CEILING, round(MAX_TOTAL_SEEDS * total_mult)),
            max_per_hop=min(PIVOT_PER_HOP_CEILING, round(MAX_SEEDS_PER_HOP * per_hop_mult)),
        )

    @staticmethod
    def _redis():
        import redis  # local import keeps the engine importable without redis

        return redis.Redis.from_url(
            os.environ.get("REDIS_URL", "redis://redis:6379/0"),
            decode_responses=True,
        )

    def mark_processed(self, case_id: str, keys: Iterable[str]) -> None:
        """Record identifiers that have already been used as seeds."""
        keys = [k for k in keys if k]
        if not keys:
            return
        try:
            client = self._redis()
            seen_key = f"socmint:pivot:seen:{case_id}"
            client.sadd(seen_key, *keys)
            client.expire(seen_key, _REDIS_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001
            logger.warning("pivot mark_processed failed: %s", exc)

    def select_new(self, case_id: str, candidates: list[PivotSeed],
                   max_total: Optional[int] = None,
                   max_per_hop: Optional[int] = None) -> list[PivotSeed]:
        """Return only candidates never seen before, honouring all caps.

        Atomically claims each identifier in the Redis visited set so concurrent
        pivot tasks can't double-process the same seed. Caps default to the
        module baselines but accept dynamic per-case overrides. Hard identifiers
        (email/phone/domain) are claimed before soft ones so the most valuable
        leads always fit inside the budget.
        """
        if not candidates:
            return []
        max_total = MAX_TOTAL_SEEDS if max_total is None else max_total
        max_per_hop = MAX_SEEDS_PER_HOP if max_per_hop is None else max_per_hop
        try:
            client = self._redis()
        except Exception as exc:  # noqa: BLE001 — without Redis, don't pivot (fail safe)
            logger.warning("pivot Redis unavailable, skipping expansion: %s", exc)
            return []

        seen_key = f"socmint:pivot:seen:{case_id}"
        count_key = f"socmint:pivot:count:{case_id}"
        client.expire(seen_key, _REDIS_TTL_SECONDS)

        already = int(client.get(count_key) or 0)
        budget = max(0, max_total - already)
        if budget <= 0:
            logger.info("pivot total cap reached for case %s", case_id)
            return []

        # Claim the highest-value identifiers first (hard before soft).
        ordered = sorted(candidates, key=lambda c: _SEED_PRIORITY.get(c.seed_type, 9))
        selected: list[PivotSeed] = []
        for cand in ordered:
            if len(selected) >= max_per_hop or len(selected) >= budget:
                break
            # SADD returns 1 only when the member is new → atomic claim.
            if client.sadd(seen_key, cand.key) == 1:
                selected.append(cand)

        if selected:
            client.incrby(count_key, len(selected))
            client.expire(count_key, _REDIS_TTL_SECONDS)
            client.expire(seen_key, _REDIS_TTL_SECONDS)
        return selected
