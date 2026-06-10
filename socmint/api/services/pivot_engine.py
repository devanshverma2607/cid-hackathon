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

# Visited-set / counter TTL so finished cases don't accumulate in Redis forever.
_REDIS_TTL_SECONDS = 24 * 3600

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_DOMAIN_RE = re.compile(r"^(?:[A-Za-z0-9\-]+\.)+[A-Za-z]{2,}$")

# platform_enrichment keys that carry pivotable identifiers.
_EMAIL_KEYS = ("public_email", "email")
_EMAIL_LIST_KEYS = ("discovered_emails", "emails")
_PHONE_KEYS = ("phone", "phone_number")
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

    def select_new(self, case_id: str, candidates: list[PivotSeed]) -> list[PivotSeed]:
        """Return only candidates never seen before, honouring all caps.

        Atomically claims each identifier in the Redis visited set so concurrent
        pivot tasks can't double-process the same seed.
        """
        if not candidates:
            return []
        try:
            client = self._redis()
        except Exception as exc:  # noqa: BLE001 — without Redis, don't pivot (fail safe)
            logger.warning("pivot Redis unavailable, skipping expansion: %s", exc)
            return []

        seen_key = f"socmint:pivot:seen:{case_id}"
        count_key = f"socmint:pivot:count:{case_id}"
        client.expire(seen_key, _REDIS_TTL_SECONDS)

        already = int(client.get(count_key) or 0)
        budget = max(0, MAX_TOTAL_SEEDS - already)
        if budget <= 0:
            logger.info("pivot total cap reached for case %s", case_id)
            return []

        selected: list[PivotSeed] = []
        for cand in candidates:
            if len(selected) >= MAX_SEEDS_PER_HOP or len(selected) >= budget:
                break
            # SADD returns 1 only when the member is new → atomic claim.
            if client.sadd(seen_key, cand.key) == 1:
                selected.append(cand)

        if selected:
            client.incrby(count_key, len(selected))
            client.expire(count_key, _REDIS_TTL_SECONDS)
            client.expire(seen_key, _REDIS_TTL_SECONDS)
        return selected
