"""Synthetic labelled identities for offline correlation validation.

Each scenario is a labelled pair of evidence groups (lists of ``EvidenceUnit``)
with a ground-truth ``same_person`` label. Scenarios are derived directly from
the documented Section 9 signal weights and thresholds, so the suite doubles as
a spec-conformance / regression guard for the scoring model. A couple of
deliberately *hard* cases keep recall honest (the engine is expected to miss
links supported only by weak corroboration).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from api.models.evidence import EvidenceUnit

_CASE = uuid.uuid4()
_RUN = uuid.uuid4()

# Identical hex pHash on both sides → Hamming distance 0 (clear photo match).
PHASH = "ffffffffffffffff"
# Deliberately dissimilar bio embeddings keep semantic similarity below the 0.80
# threshold WITHOUT loading a sentence-transformer model, so the stylometry
# fallback is exercised deterministically and fast.
_EMB_A = [1.0, 0.0, 0.0, 0.0]
_EMB_B = [0.0, 1.0, 0.0, 0.0]
# CLIP avatar embeddings: A and B are the SAME image re-encoded (cosine ~0.985,
# >= IMAGE_SIM_THRESHOLD 0.92 -> photo match); DISSIM is a different image.
_IMG_A = [1.0, 0.0, 0.0, 0.0]
_IMG_B = [0.985, 0.174, 0.0, 0.0]
_IMG_DISSIM = [0.0, 0.0, 1.0, 0.0]

# FaceNet face embeddings: FACE_A/FACE_B are the SAME person in different photos
# (cosine ~0.80, >= FACE_SIM_THRESHOLD 0.62 -> match); FACE_DISSIM is another
# person.
_FACE_A = [1.0, 0.0, 0.0, 0.0]
_FACE_B = [0.80, 0.60, 0.0, 0.0]
_FACE_DISSIM = [0.0, 0.0, 1.0, 0.0]


def _unit(
    *,
    result_type: str = "account_found",
    result_value: str = "https://github.com/x",
    seed_type: str = "username",
    seed_value: str = "",
    source_platform: str = "github",
    enrichment: dict | None = None,
    bio_embedding: list[float] | None = None,
    image_embedding: list[float] | None = None,
    face_embedding: list[float] | None = None,
    age_days: int = 0,
) -> EvidenceUnit:
    """Build one synthetic EvidenceUnit (age 0 → full, decay-free signal weight)."""
    return EvidenceUnit(
        case_id=_CASE,
        run_id=_RUN,
        tool_name="synthetic",
        tool_version="1.0",
        tool_tier=1,
        source_platform=source_platform,
        source_tier=2,
        seed_type=seed_type,
        seed_value=seed_value,
        result_type=result_type,
        result_value=result_value,
        analyst_id="validator",
        timestamp_collected=datetime.now(timezone.utc) - timedelta(days=age_days),
        platform_enrichment=enrichment,
        bio_embedding=bio_embedding,
        image_embedding=image_embedding,
        face_embedding=face_embedding,
    )


def acct(
    *,
    handle: str | None = None,
    email: str | None = None,
    url: str | None = None,
    photo: str | None = None,
    bio: str | None = None,
    bio_embedding: list[float] | None = None,
    image_embedding: list[float] | None = None,
    face_embedding: list[float] | None = None,
    join_date: str | None = None,
    location: str | None = None,
    tz: str | None = None,
    language: str | None = None,
    google: bool = False,
    gravatar: bool = False,
    whatsapp: bool = False,
    breach: str | None = None,
    dork: bool = False,
    platform: str = "github",
) -> list[EvidenceUnit]:
    """Assemble an account evidence group from the requested signals."""
    enrich: dict = {}
    if bio is not None:
        enrich["bio"] = bio
    if photo is not None:
        enrich["phash"] = photo
    if join_date is not None:
        enrich["join_date"] = join_date
    if location is not None:
        enrich["location"] = location
    if tz is not None:
        enrich["timezone"] = tz
    if language is not None:
        enrich["language"] = language

    units = [
        _unit(
            result_type="account_found",
            result_value=(url or f"https://{platform}/{handle or 'user'}"),
            seed_type="username",
            seed_value=(handle or ""),
            source_platform=platform,
            enrichment=(enrich or None),
            bio_embedding=bio_embedding,
            image_embedding=image_embedding,
            face_embedding=face_embedding,
        )
    ]
    if email:
        units.append(_unit(result_type="email_registered", result_value=email,
                           seed_type="email", seed_value=email, source_platform=platform))
    if google:
        units.append(_unit(result_type="google_hit", result_value="google-account",
                           source_platform="google"))
    if gravatar:
        units.append(_unit(result_type="gravatar_hit", result_value="gravatar",
                           source_platform="gravatar"))
    if whatsapp:
        units.append(_unit(result_type="whatsapp_hit", result_value="whatsapp",
                           source_platform="whatsapp"))
    if breach:
        units.append(_unit(result_type="breach_hit", result_value=breach,
                           source_platform="breachdb"))
    if dork:
        units.append(_unit(result_type="dork_hit", result_value="https://web/dork-hit",
                           source_platform="web"))
    return units


@dataclass(frozen=True)
class Scenario:
    name: str
    same_person: bool
    units_a: list[EvidenceUnit]
    units_b: list[EvidenceUnit]
    note: str = ""


# Same-author / different-author style bios (>= 24 chars) for the stylometry case.
# Same distinctive phrasing, clauses reordered → high stylometric cosine on
# genuinely different surface text.
_STYLE_A = "infosec researcher. breaking things to make them safer. ctf player by night."
_STYLE_B = "infosec researcher by day, ctf player by night. breaking things to make them safer."


def scenarios() -> list[Scenario]:
    """The labelled synthetic corpus."""
    return [
        # ---- POSITIVES (same person → expected: a link, tier != DISCARD) -----
        Scenario("handle+email", True,
                 acct(handle="nightowl_dev", email="nightowl@example.com", platform="github"),
                 acct(handle="nightowl_dev", email="nightowl@example.com", platform="gitlab"),
                 "identical handle + email"),
        Scenario("handle+photo", True,
                 acct(handle="pixel_sam", photo=PHASH, platform="github"),
                 acct(handle="pixel_sam", photo=PHASH, platform="reddit"),
                 "identical handle + reused avatar"),
        Scenario("email+breach", True,
                 acct(handle="aria.k", email="aria.k@example.com", breach="acme-2019", platform="x"),
                 acct(handle="ariak2", email="aria.k@example.com", breach="acme-2019", platform="instagram"),
                 "shared email + shared breach source"),
        Scenario("photo+breach", True,
                 acct(handle="rover", photo=PHASH, breach="leakco-2020", platform="github"),
                 acct(handle="wanderer", photo=PHASH, breach="leakco-2020", platform="gitlab"),
                 "reused avatar + shared breach (different handles)"),
        Scenario("semantic-photo-reuse", True,
                 acct(handle="lenswork", image_embedding=_IMG_A, breach="snapleak-2021", platform="github"),
                 acct(handle="framegrab", image_embedding=_IMG_B, breach="snapleak-2021", platform="gitlab"),
                 "same avatar re-encoded (CLIP match, no pHash) + shared breach"),
        Scenario("same-face-different-photo", True,
                 acct(handle="shutterbug", face_embedding=_FACE_A, breach="camleak-2022", platform="github"),
                 acct(handle="frameone", face_embedding=_FACE_B, breach="camleak-2022", platform="gitlab"),
                 "same person, different photos (FaceNet match) + shared breach"),
        Scenario("handle+temporal", True,
                 acct(handle="orbital_kt", join_date="2020-03-01", platform="github"),
                 acct(handle="orbital_kt", join_date="2020-03-18", platform="reddit"),
                 "identical handle + creation within 17 days"),
        Scenario("handle+stylometry", True,
                 acct(handle="quietquill", bio=_STYLE_A, bio_embedding=_EMB_A, platform="github"),
                 acct(handle="quietquill", bio=_STYLE_B, bio_embedding=_EMB_B, platform="medium"),
                 "identical handle + same writing style, different bio text"),
        Scenario("strong-medium", True,
                 acct(handle="delta_jay", email="delta.jay@example.com", photo=PHASH, platform="github"),
                 acct(handle="delta_jay", email="delta.jay@example.com", photo=PHASH, platform="gitlab"),
                 "handle + email + avatar (expected MEDIUM/HIGH)"),
        Scenario("very-strong-high", True,
                 acct(handle="cipher9", email="cipher9@example.com", breach="megaleak-2018",
                      google=True, platform="github"),
                 acct(handle="cipher9", email="cipher9@example.com", breach="megaleak-2018",
                      google=True, platform="gitlab"),
                 "handle + email + breach + google (expected HIGH)"),
        # Deliberately HARD positive: only weak corroboration (style + timing),
        # below the LOW cut-off — the engine is expected to MISS this (FN). Keeps
        # recall honest rather than trivially 1.0.
        Scenario("hard-weak-positive", True,
                 acct(handle="emberlark", bio=_STYLE_A, bio_embedding=_EMB_A,
                      join_date="2021-05-01", platform="github"),
                 acct(handle="frostvane", bio=_STYLE_B, bio_embedding=_EMB_B,
                      join_date="2021-05-20", platform="medium"),
                 "different handles, only weak style+timing evidence (hard miss)"),

        # ---- NEGATIVES (different people → expected: DISCARD) ----------------
        Scenario("unrelated", False,
                 acct(handle="alpha_one", email="alpha@example.com", photo="0000000000000000", platform="github"),
                 acct(handle="bravo_two", email="bravo@other.com", photo="ffffffff00000000", platform="reddit"),
                 "nothing in common"),
        Scenario("single-google", False,
                 acct(handle="solo_a", google=True, platform="github"),
                 acct(handle="solo_b", google=True, platform="gitlab"),
                 "only both have a google account (1 signal → 2-signal rule)"),
        Scenario("weak-two", False,
                 acct(handle="misc_a", whatsapp=True, gravatar=True, platform="github"),
                 acct(handle="misc_b", whatsapp=True, gravatar=True, platform="reddit"),
                 "two weak bonus signals (7+8=15) below LOW threshold"),
        Scenario("levenshtein-conflict", False,
                 acct(handle="marker01", tz="2", language="en", platform="github"),
                 acct(handle="marker02", tz="11", language="ja", platform="reddit"),
                 "near handles but timezone + language conflict (1 signal + penalties)"),
        Scenario("location-only", False,
                 acct(handle="cityp_a", location="Pune, IN", platform="github"),
                 acct(handle="cityp_b", location="Pune, IN", platform="reddit"),
                 "only a shared location (1 enrichment signal)"),
        Scenario("temporal-only", False,
                 acct(handle="zeta_alpha", join_date="2022-01-03", platform="github"),
                 acct(handle="zeta_bravo", join_date="2022-01-15", platform="reddit"),
                 "two different people who happened to sign up the same week (1 signal)"),
    ]
