"""MODULE 6 — Correlation Engine.

Scores identity links between accounts using the exact weighted-signal model in
Section 9 of SOCMINT_PLAN_v2_0.txt: positive signal weights, conflict penalties,
evidence decay, the 2-signal corroboration rule, and confidence thresholds.
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from api.models.evidence import EvidenceUnit
from api.models.identity_link import IdentityLink
from api.services.normaliser import USERNAME_BLACKLIST, leet_normalise

logger = logging.getLogger(__name__)

# ---- Section 9.2 — positive signal weights ---------------------------------
W_USERNAME_EXACT = 25
W_EMAIL_MATCH = 20
W_BREACH_REUSE = 18
W_PHOTO_MATCH = 15
W_GOOGLE = 12
W_BIO_SIM = 10
W_URL_OVERLAP = 10
W_GRAVATAR = 8
W_WHATSAPP = 7
W_LEVENSHTEIN = 5
W_DORK = 5
W_ENRICHMENT = 5
W_STYLOMETRY = 8   # author writing-style fingerprint (complements bio semantics)
W_TEMPORAL = 6     # account-creation proximity (same owner, same era)

# ---- Section 9.3 — conflict penalties --------------------------------------
P_BOT = -15
P_TIMEZONE = -10
P_LANGUAGE = -8
P_BLACKLIST = -8
P_IMPOSSIBLE_DATE = -5
P_DUP_USERNAMES = -5

# ---- Section 9.5 — decay periods (days) and floors -------------------------
DECAY = {
    "breach": (1095.0, 0.20),
    "email_platform": (730.0, 0.30),
    "username": (365.0, 0.50),
    "photo": (180.0, 0.60),
    "bio": (90.0, 0.70),
    "stylometry": (90.0, 0.70),
    "dork": (30.0, 0.50),
}

# ---- Section 9.4 — thresholds ----------------------------------------------
THRESHOLD_HIGH = 75
THRESHOLD_MEDIUM = 50
THRESHOLD_LOW = 25

PHASH_MAX_DISTANCE = 8
IMAGE_SIM_THRESHOLD = 0.92   # CLIP cosine: same avatar re-encoded/resized/cropped
FACE_SIM_THRESHOLD = 0.62    # FaceNet cosine: same person, different photo
BIO_SIM_THRESHOLD = 0.80
LEVENSHTEIN_MAX = 2
MIN_SIGNALS = 2

# ---- engine identity + new-signal tuning -----------------------------------
ENGINE_VERSION = "correlation-2.2"
STYLOMETRY_THRESHOLD = 0.68   # conservative: short social bios are noisy
STYLOMETRY_MIN_LEN = 24       # need enough text to fingerprint a style
CREATION_PROXIMITY_DAYS = 45  # accounts created within ~6 weeks corroborate


def _decay_factor(age_days: int, signal: str) -> float:
    period, floor = DECAY[signal]
    return max(floor, 1.0 - (age_days / period))


def _age_days(collected: datetime) -> int:
    now = datetime.now(timezone.utc)
    if collected.tzinfo is None:
        collected = collected.replace(tzinfo=timezone.utc)
    return max(0, (now - collected).days)


def _handle_from_value(value: str) -> str:
    """Extract a bare handle from a URL or raw value, then leet-normalise."""
    candidate = value or ""
    if "://" in candidate:
        candidate = candidate.rstrip("/").rsplit("/", 1)[-1]
    return leet_normalise(candidate.lstrip("@"))


def _parse_vector(value) -> Optional[list[float]]:
    """Parse a pgvector column value (str '[..]' / list / None) into floats."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [float(x) for x in value] or None
    text_val = str(value).strip().strip("[]")
    if not text_val:
        return None
    try:
        return [float(x) for x in text_val.split(",") if x.strip()] or None
    except ValueError:
        return None


class CorrelationEngine:
    """Compute pairwise confidence and persist identity links."""

    # ---- feature extraction -------------------------------------------------
    def _extract_profile(self, units: list[EvidenceUnit]) -> dict:
        """Reduce a group of evidence units into comparable features."""
        profile = {
            "usernames": set(),
            "emails": set(),
            "urls": set(),
            "has_google": False,
            "has_gravatar": False,
            "has_whatsapp": False,
            "breach_sources": set(),
            "has_dork": False,
            "bio": None,
            "bio_embedding": None,
            "image_embedding": None,
            "face_embedding": None,
            "photo_hash": None,
            "location": None,
            "join_date": None,
            "timezone": None,
            "language": None,
            "is_bot": False,
            "dup_usernames_platform": False,
            "min_age_days": 0,
            "platform": units[0].source_platform if units else "",
        }
        ages = []
        for unit in units:
            ages.append(_age_days(unit.timestamp_collected))
            if unit.result_type == "account_found":
                profile["usernames"].add(_handle_from_value(unit.result_value))
                if "://" in unit.result_value:
                    profile["urls"].add(unit.result_value.lower())
            if unit.seed_type == "username" and unit.seed_value:
                profile["usernames"].add(leet_normalise(unit.seed_value))
            if unit.result_type == "email_registered" or unit.seed_type == "email":
                if unit.seed_value and "@" in unit.seed_value:
                    profile["emails"].add(unit.seed_value.lower())
            if unit.result_type == "google_hit":
                profile["has_google"] = True
            if unit.result_type == "gravatar_hit":
                profile["has_gravatar"] = True
            if unit.result_type == "whatsapp_hit":
                profile["has_whatsapp"] = True
            if unit.result_type == "breach_hit":
                profile["breach_sources"].add(unit.result_value)
            if unit.result_type == "dork_hit":
                profile["has_dork"] = True
            if unit.bio_embedding:
                profile["bio_embedding"] = unit.bio_embedding
            if unit.image_embedding and not profile["image_embedding"]:
                profile["image_embedding"] = unit.image_embedding
            if unit.face_embedding and not profile["face_embedding"]:
                profile["face_embedding"] = unit.face_embedding

            enrichment = unit.platform_enrichment or {}
            if isinstance(enrichment, dict):
                profile["bio"] = profile["bio"] or enrichment.get("bio") or enrichment.get("description")
                profile["photo_hash"] = profile["photo_hash"] or enrichment.get("phash") or enrichment.get("profile_pic_hash")
                profile["location"] = profile["location"] or enrichment.get("location")
                profile["join_date"] = profile["join_date"] or enrichment.get("join_date") or enrichment.get("created_at")
                profile["timezone"] = profile["timezone"] or enrichment.get("timezone")
                profile["language"] = profile["language"] or enrichment.get("language")
                if enrichment.get("is_bot"):
                    profile["is_bot"] = True
                if enrichment.get("allows_duplicate_usernames"):
                    profile["dup_usernames_platform"] = True

        profile["min_age_days"] = min(ages) if ages else 0
        profile["usernames"].discard("")
        return profile

    # ---- scoring ------------------------------------------------------------
    def compute_confidence(
        self, units_a: list[EvidenceUnit], units_b: list[EvidenceUnit]
    ) -> dict:
        """Compute the confidence score between two account evidence groups."""
        a = self._extract_profile(units_a)
        b = self._extract_profile(units_b)
        age = max(a["min_age_days"], b["min_age_days"])

        breakdown: dict[str, dict] = {}
        score = 0.0

        def add_signal(name: str, weight: float, decay: float = 1.0) -> None:
            nonlocal score
            contribution = weight * decay
            breakdown[name] = {"weight": weight, "decay_factor": round(decay, 4),
                               "contribution": round(contribution, 4)}
            score += contribution

        # Identical username (exact, normalised) — durable, no decay.
        shared_usernames = a["usernames"] & b["usernames"]
        if shared_usernames:
            add_signal("identical_username", W_USERNAME_EXACT)
        # Username Levenshtein distance <= 2.
        elif self._min_levenshtein(a["usernames"], b["usernames"]) <= LEVENSHTEIN_MAX:
            add_signal("username_levenshtein", W_LEVENSHTEIN)

        # Same email or +alias variation — durable, no decay.
        if self._emails_match(a["emails"], b["emails"]):
            add_signal("email_match", W_EMAIL_MATCH)

        # Breach credential reuse (H8mail) — decay applied.
        if a["breach_sources"] & b["breach_sources"]:
            add_signal("breach_reuse", W_BREACH_REUSE, _decay_factor(age, "breach"))

        # Identical profile photo — pHash (<=8) OR CLIP reverse-image embedding
        # (the same avatar re-encoded/resized/cropped). Decay applied; one signal.
        if self._photo_match(a["photo_hash"], b["photo_hash"]) or self._image_embedding_match(
            a.get("image_embedding"), b.get("image_embedding")
        ) or self._face_match(a.get("face_embedding"), b.get("face_embedding")):
            add_signal("photo_match", W_PHOTO_MATCH, _decay_factor(age, "photo"))

        # Ghunt Google account confirmed — bonus, no decay.
        if a["has_google"] and b["has_google"]:
            add_signal("google_confirmed", W_GOOGLE)

        # Bio similarity >= 80% (semantic) — decay applied. When the bios are
        # not semantically close (or no embedding is available), fall back to a
        # stylometric fingerprint that catches the *same author writing
        # different text* (char n-gram + word-usage cosine), so the two signals
        # never double-count the same piece of text.
        sim = self._bio_similarity(a, b)
        if sim is not None and sim >= BIO_SIM_THRESHOLD:
            add_signal("bio_similarity", W_BIO_SIM, _decay_factor(age, "bio"))
        else:
            sty = self._stylometry_similarity(a.get("bio"), b.get("bio"))
            if sty is not None and sty >= STYLOMETRY_THRESHOLD:
                add_signal("stylometry_match", W_STYLOMETRY, _decay_factor(age, "stylometry"))

        # Overlapping linked external URLs — durable, no decay.
        if a["urls"] & b["urls"]:
            add_signal("url_overlap", W_URL_OVERLAP)

        # Gravatar profile confirmed — bonus, no decay.
        if a["has_gravatar"] and b["has_gravatar"]:
            add_signal("gravatar_confirmed", W_GRAVATAR)

        # WhatsApp linkage confirmed — bonus, no decay.
        if a["has_whatsapp"] and b["has_whatsapp"]:
            add_signal("whatsapp_confirmed", W_WHATSAPP)

        # Dork hit confirming name + platform — decay applied.
        if a["has_dork"] and b["has_dork"]:
            add_signal("dork_corroboration", W_DORK, _decay_factor(age, "dork"))

        # Platform enrichment metadata match (bio/location/join date overlap).
        if self._enrichment_match(a, b):
            add_signal("enrichment_match", W_ENRICHMENT)

        # Account-creation proximity — two profiles created within a short
        # window corroborate common ownership (durable, no decay).
        if self._creation_proximity(a.get("join_date"), b.get("join_date")):
            add_signal("creation_proximity", W_TEMPORAL)

        # ---- conflict penalties --------------------------------------------
        penalties: dict[str, int] = {}

        def add_penalty(name: str, value: int) -> None:
            nonlocal score
            penalties[name] = value
            score += value

        if a["is_bot"] or b["is_bot"]:
            add_penalty("bot_detected", P_BOT)
        if self._timezone_mismatch(a["timezone"], b["timezone"]):
            add_penalty("timezone_mismatch", P_TIMEZONE)
        if self._language_mismatch(a["language"], b["language"]):
            add_penalty("language_mismatch", P_LANGUAGE)
        if (a["usernames"] | b["usernames"]) & USERNAME_BLACKLIST:
            add_penalty("blacklist_username", P_BLACKLIST)
        if self._impossible_creation_date(a["join_date"], b["join_date"]):
            add_penalty("impossible_creation_date", P_IMPOSSIBLE_DATE)
        if a["dup_usernames_platform"] or b["dup_usernames_platform"]:
            add_penalty("duplicate_usernames_allowed", P_DUP_USERNAMES)

        signal_count = len(breakdown)
        tier = self._tier(score, signal_count)
        probability = self._calibrate(score)

        # Reproducibility + calibrated confidence travel with the link in a
        # reserved key (never counted as a signal; consumers ignore '_'-keys).
        breakdown["_meta"] = {
            "engine_version": ENGINE_VERSION,
            "probability": probability,
            "score": round(score, 2),
            "signal_count": signal_count,
        }

        return {
            "confidence_score": round(score, 2),
            "confidence_tier": tier,
            "signal_breakdown": breakdown,
            "penalties": penalties,
            "signal_count": signal_count,
            "probability": probability,
            "engine_version": ENGINE_VERSION,
        }

    @staticmethod
    def _tier(score: float, signal_count: int) -> str:
        # 2-signal minimum rule (non-negotiable).
        if signal_count < MIN_SIGNALS:
            return "DISCARD"
        if score >= THRESHOLD_HIGH:
            return "HIGH"
        if score >= THRESHOLD_MEDIUM:
            return "MEDIUM"
        if score >= THRESHOLD_LOW:
            return "LOW"
        return "DISCARD"

    # ---- comparison helpers -------------------------------------------------
    @staticmethod
    def _min_levenshtein(set_a: set, set_b: set) -> int:
        if not set_a or not set_b:
            return 99
        try:
            import Levenshtein

            return min(Levenshtein.distance(x, y) for x in set_a for y in set_b)
        except Exception:
            # Fallback: exact match => 0 else large.
            return 0 if set_a & set_b else 99

    @staticmethod
    def _emails_match(set_a: set, set_b: set) -> bool:
        if set_a & set_b:
            return True

        def base(email: str) -> str:
            local, _, domain = email.partition("@")
            local = local.split("+", 1)[0]
            return f"{local}@{domain}"

        return bool({base(e) for e in set_a} & {base(e) for e in set_b})

    @staticmethod
    def _photo_match(hash_a: Optional[str], hash_b: Optional[str]) -> bool:
        if not hash_a or not hash_b:
            return False
        try:
            import imagehash

            return (imagehash.hex_to_hash(hash_a) - imagehash.hex_to_hash(hash_b)) <= PHASH_MAX_DISTANCE
        except Exception:
            return hash_a == hash_b

    @staticmethod
    def _image_embedding_match(
        emb_a: Optional[list[float]], emb_b: Optional[list[float]]
    ) -> bool:
        """True when two CLIP avatar embeddings are the same image (cosine high)."""
        if not emb_a or not emb_b or len(emb_a) != len(emb_b):
            return False
        return CorrelationEngine._cosine(emb_a, emb_b) >= IMAGE_SIM_THRESHOLD

    @staticmethod
    def _face_match(
        emb_a: Optional[list[float]], emb_b: Optional[list[float]]
    ) -> bool:
        """True when two FaceNet embeddings are the same person (cosine high)."""
        if not emb_a or not emb_b or len(emb_a) != len(emb_b):
            return False
        return CorrelationEngine._cosine(emb_a, emb_b) >= FACE_SIM_THRESHOLD

    @staticmethod
    def _bio_similarity(a: dict, b: dict) -> Optional[float]:
        emb_a, emb_b = a.get("bio_embedding"), b.get("bio_embedding")
        if emb_a and emb_b:
            return CorrelationEngine._cosine(emb_a, emb_b)
        if a.get("bio") and b.get("bio"):
            try:
                from sentence_transformers import SentenceTransformer, util

                model = SentenceTransformer("all-MiniLM-L6-v2")
                vecs = model.encode([a["bio"], b["bio"]])
                return float(util.cos_sim(vecs[0], vecs[1]))
            except Exception:
                return None
        return None

    @staticmethod
    def _cosine(v1: list[float], v2: list[float]) -> float:
        dot = sum(x * y for x, y in zip(v1, v2))
        n1 = math.sqrt(sum(x * x for x in v1))
        n2 = math.sqrt(sum(y * y for y in v2))
        return dot / (n1 * n2) if n1 and n2 else 0.0

    @staticmethod
    def _stylometry_similarity(bio_a: Optional[str], bio_b: Optional[str]) -> Optional[float]:
        """Author writing-style similarity via char n-gram + word-usage cosine.

        Dependency-free. Captures *style* (n-gram/word habits) rather than
        meaning, so it complements semantic bio similarity by catching the same
        author writing two different bios. Returns ``None`` when either bio is
        missing or too short to fingerprint reliably.
        """
        if not bio_a or not bio_b:
            return None
        a, b = bio_a.strip(), bio_b.strip()
        if len(a) < STYLOMETRY_MIN_LEN or len(b) < STYLOMETRY_MIN_LEN:
            return None

        def features(text: str) -> Counter:
            t = " ".join(text.lower().split())
            grams: Counter = Counter(t[i:i + 3] for i in range(len(t) - 2))
            grams.update("w:" + w for w in re.findall(r"[a-z']+", t))
            return grams

        fa, fb = features(a), features(b)
        common = set(fa) & set(fb)
        if not common:
            return 0.0
        dot = sum(fa[k] * fb[k] for k in common)
        na = math.sqrt(sum(v * v for v in fa.values()))
        nb = math.sqrt(sum(v * v for v in fb.values()))
        return dot / (na * nb) if na and nb else 0.0

    @staticmethod
    def _parse_date(value) -> Optional[datetime]:
        """Best-effort parse of a creation/join date in common formats."""
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            return value
        s = str(value).strip()
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            pass
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%m/%d/%Y",
                    "%b %d, %Y", "%B %d, %Y", "%b %Y", "%B %Y", "%Y"):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                continue
        return None

    @classmethod
    def _creation_proximity(cls, date_a, date_b) -> bool:
        da, db = cls._parse_date(date_a), cls._parse_date(date_b)
        if not da or not db:
            return False
        if da.tzinfo and not db.tzinfo:
            db = db.replace(tzinfo=da.tzinfo)
        elif db.tzinfo and not da.tzinfo:
            da = da.replace(tzinfo=db.tzinfo)
        return abs((da - db).days) <= CREATION_PROXIMITY_DAYS

    @staticmethod
    def _calibrate(score: float) -> float:
        """Map a raw additive score to a calibrated [0,1] probability.

        Logistic centred on the MEDIUM threshold so score=50→0.5, 75→~0.88,
        25→~0.12 — a monotonic, explainable confidence companion to the tier.
        """
        return round(1.0 / (1.0 + math.exp(-0.08 * (score - 50.0))), 4)

    @staticmethod
    def _enrichment_match(a: dict, b: dict) -> bool:
        matches = 0
        for key in ("location", "join_date"):
            if a.get(key) and a.get(key) == b.get(key):
                matches += 1
        if a.get("bio") and a.get("bio") == b.get("bio"):
            matches += 1
        return matches >= 1

    @staticmethod
    def _timezone_mismatch(tz_a, tz_b) -> bool:
        if tz_a is None or tz_b is None:
            return False
        try:
            return abs(float(tz_a) - float(tz_b)) > 4
        except (TypeError, ValueError):
            return str(tz_a) != str(tz_b)

    @staticmethod
    def _language_mismatch(lang_a, lang_b) -> bool:
        if not lang_a or not lang_b:
            return False
        return str(lang_a).lower() != str(lang_b).lower()

    @staticmethod
    def _impossible_creation_date(date_a, date_b) -> bool:
        # Placeholder temporal logic: flag if either is an explicit impossible marker.
        for value in (date_a, date_b):
            if isinstance(value, str) and value.strip().lower() in ("impossible", "invalid"):
                return True
        return False

    # ---- persistence --------------------------------------------------------
    def run_full_correlation(self, case_id: UUID, session: Session) -> list[IdentityLink]:
        """Load evidence, score cross-platform pairs, persist HIGH/MEDIUM links."""
        from api.services.graph_builder import GraphBuilder

        rows = session.execute(
            text("SELECT * FROM evidence_units WHERE case_id = :cid"),
            {"cid": str(case_id)},
        ).mappings().all()

        groups: dict[str, list[EvidenceUnit]] = {}
        for row in rows:
            unit = self._row_to_unit(dict(row))
            groups.setdefault(unit.source_platform, []).append(unit)

        platforms = sorted(groups.keys())
        links: list[IdentityLink] = []
        graph = GraphBuilder()

        for i in range(len(platforms)):
            for j in range(i + 1, len(platforms)):
                pa, pb = platforms[i], platforms[j]
                result = self.compute_confidence(groups[pa], groups[pb])

                # 2-signal rule: never persist a single-signal link.
                if result["signal_count"] < MIN_SIGNALS:
                    continue
                if result["confidence_tier"] == "DISCARD":
                    continue

                link = IdentityLink(
                    case_id=case_id,
                    account_a=self._account_label(groups[pa]),
                    account_b=self._account_label(groups[pb]),
                    platform_a=pa,
                    platform_b=pb,
                    confidence_score=result["confidence_score"],
                    confidence_tier=result["confidence_tier"],
                    signal_breakdown=result["signal_breakdown"],
                    signal_count=result["signal_count"],
                )
                self._persist_link(link, session)
                links.append(link)

                # Write HIGH and MEDIUM links to Neo4j.
                if result["confidence_tier"] in ("HIGH", "MEDIUM"):
                    try:
                        graph.upsert_identity_link(link)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("neo4j link write failed: %s", exc)

        # Partition the freshly-written SAME_AS graph into communities (GDS
        # Louvain when available, label-propagation fallback) — best-effort.
        try:
            graph.detect_communities(case_id, write_back=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("community detection failed: %s", exc)

        return links

    @staticmethod
    def _account_label(units: list[EvidenceUnit]) -> str:
        for unit in units:
            if unit.result_type == "account_found":
                return unit.result_value
        return units[0].result_value if units else ""

    def _persist_link(self, link: IdentityLink, session: Session) -> None:
        import json

        session.execute(
            text(
                """
                INSERT INTO identity_links (
                    link_id, case_id, account_a, account_b, platform_a, platform_b,
                    confidence_score, confidence_tier, signal_breakdown, signal_count
                ) VALUES (
                    :link_id, :case_id, :account_a, :account_b, :platform_a, :platform_b,
                    :confidence_score, :confidence_tier, CAST(:signal_breakdown AS JSONB), :signal_count
                )
                """
            ),
            {
                "link_id": str(link.link_id),
                "case_id": str(link.case_id),
                "account_a": link.account_a,
                "account_b": link.account_b,
                "platform_a": link.platform_a,
                "platform_b": link.platform_b,
                "confidence_score": link.confidence_score,
                "confidence_tier": link.confidence_tier,
                "signal_breakdown": json.dumps(link.signal_breakdown),
                "signal_count": link.signal_count,
            },
        )
        session.commit()

    @staticmethod
    def _row_to_unit(row: dict) -> EvidenceUnit:
        return EvidenceUnit(
            evidence_id=row["evidence_id"],
            case_id=row["case_id"],
            run_id=row["run_id"],
            tool_name=row["tool_name"],
            tool_version=row["tool_version"],
            tool_tier=row["tool_tier"],
            source_platform=row["source_platform"],
            source_tier=row["source_tier"],
            seed_type=row["seed_type"],
            seed_value=row["seed_value"],
            result_type=row["result_type"],
            result_value=row["result_value"],
            confidence_raw=row.get("confidence_raw"),
            signal_weights=row.get("signal_weights"),
            image_embedding=_parse_vector(row.get("image_embedding")),
            face_embedding=_parse_vector(row.get("face_embedding")),
            timestamp_collected=row["timestamp_collected"],
            timestamp_preserved=row.get("timestamp_preserved"),
            snapshot_ref=row.get("snapshot_ref"),
            snapshot_hash=row.get("snapshot_hash"),
            wayback_ref=row.get("wayback_ref"),
            platform_enrichment=row.get("platform_enrichment"),
            analyst_id=row["analyst_id"],
            notes=row.get("notes"),
        )
