"""MODULE 7 — Identity Resolution / Persona Clustering Engine.

The correlation engine (MODULE 6) scores *pairwise* identity links. This engine
goes one step further: it fuses every signal already collected for a case and
clusters the scattered platform accounts into distinct **human personas** — the
question a real analyst actually asks ("how many people am I looking at, and
which accounts belong to each one?").

It is deliberately self-contained and fast (pure Python, no ML model load):

  1. Build *accounts* from positive evidence units (one presence per platform).
  2. Normalise identifiers, including decoding email-shaped handles so a
     username that is literally an email bridges to the email seed.
  3. Score every account pair with explainable, weighted signals.
  4. Merge accounts joined by a strong (hard) identifier or a high combined
     weight using union-find, yielding connected personas.
  5. Summarise each persona: confidence tier, the pivot identifier that holds it
     together, the linking signals, cross-platform reach, and a timeline.
"""
from __future__ import annotations

import difflib
import math
import re
from collections import defaultdict
from datetime import datetime
from typing import Optional
from urllib.parse import unquote
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from api.services.normaliser import USERNAME_BLACKLIST, leet_normalise

# ---- signal weights (aligned with the correlation model, Section 9.2) -------
W_SHARED_EMAIL = 30
W_SHARED_PHONE = 30
W_SHARED_USERNAME = 25
W_PHOTO_MATCH = 20
W_HANDLE_EMAIL = 18  # a username equals another account's email local-part
W_SHARED_URL = 12
W_USERNAME_SIM = 8  # soft — corroborates, does not merge alone
W_BIO_SIM = 6  # soft

# An edge merges two accounts when it carries a hard identifier OR clears this.
MERGE_THRESHOLD = 18

# Cap the edge list returned to clients. The full pairwise set is used
# internally for clustering, but a case with hundreds of accounts produces tens
# of thousands of edges — far too many to ship over the wire or render. We
# return a bounded, visualisation-friendly subset (merge edges first, then the
# strongest corroborating edges) so the payload stays small and the persona
# scores/signal counts (computed from the full set) remain exact.
_MAX_PUBLIC_EDGES = 1200

# Alternative-hypothesis tuning: a cross-persona near-miss is only worth showing
# when it carries a meaningful (hard, or >= this weight) signal, and we cap how
# many competing hypotheses we surface per persona to keep the payload small.
_HYPOTHESIS_MIN_WEIGHT = 8
_MAX_HYPOTHESES_PER_PERSONA = 3

HARD_SIGNALS = {
    "shared_email", "shared_phone", "shared_username",
    "photo_match", "handle_email_match", "shared_url",
}

USERNAME_SIM_THRESHOLD = 0.86
BIO_SIM_THRESHOLD = 0.60
PHASH_MAX_DISTANCE = 8
IMAGE_SIM_THRESHOLD = 0.92   # CLIP cosine: same avatar re-encoded/resized/cropped
FACE_SIM_THRESHOLD = 0.62    # FaceNet cosine: same person, different photo

# enrichment keys to mine for identifiers
_EMAIL_KEYS = ("email", "public_email")
_EMAIL_LIST_KEYS = ("emails", "discovered_emails")
_PHONE_KEYS = ("phone", "phone_number", "e164")
_USERNAME_KEYS = ("username", "twitter_username", "telegram_username", "handle")
_USERNAME_LIST_KEYS = ("discovered_usernames",)
_URL_KEYS = ("website", "blog", "external_url")
_URL_LIST_KEYS = ("links", "urls")
_BIO_KEYS = ("bio", "description", "headline", "about")
_PHASH_KEYS = ("phash", "profile_pic_hash")

_SIGNAL_LABELS = {
    "shared_email": "Shared email address",
    "shared_phone": "Shared phone number",
    "shared_username": "Reused username",
    "photo_match": "Matching profile photo",
    "handle_email_match": "Username equals email handle",
    "shared_url": "Shared external link",
    "username_similar": "Similar username",
    "bio_similar": "Similar bio text",
}


def _norm_phone(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    digits = re.sub(r"[^0-9]", "", str(value))
    return f"+{digits}" if len(digits) >= 7 else None


def _norm_email(value: Optional[str]) -> Optional[str]:
    if not value or "@" not in value:
        return None
    value = value.strip().lower()
    return value if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value) else None


def _email_localpart(email: str) -> str:
    return email.split("@", 1)[0].split("+", 1)[0]


def _clean_username(value: str) -> Optional[str]:
    """Leet-normalised match key for a handle (robust to 1337-speak)."""
    norm = leet_normalise((value or "").lstrip("@").strip())
    if not norm or norm in USERNAME_BLACKLIST or len(norm) < 3:
        return None
    return norm


def _display_username(value: str) -> str:
    """Human-readable form of a handle (original spelling, no leet mangling)."""
    return (value or "").lstrip("@").strip().lower()


def _calibrate_persona(score: float) -> float:
    """Persona score (0-99) → calibrated [0,1] confidence (logistic centred @50)."""
    return round(1.0 / (1.0 + math.exp(-0.06 * (score - 50.0))), 4)


def _cosine(v1: list[float], v2: list[float]) -> float:
    dot = sum(x * y for x, y in zip(v1, v2))
    n1 = math.sqrt(sum(x * x for x in v1))
    n2 = math.sqrt(sum(y * y for y in v2))
    return dot / (n1 * n2) if n1 and n2 else 0.0


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


class PersonaResolver:
    """Cluster a case's accounts into confidence-scored human personas."""

    # ---- 1. load ------------------------------------------------------------
    def _load_units(self, case_id: UUID, session: Session) -> list[dict]:
        rows = session.execute(
            text(
                "SELECT evidence_id, tool_name, source_platform, seed_type, "
                "seed_value, result_type, result_value, platform_enrichment, "
                "image_embedding, face_embedding, timestamp_collected "
                "FROM evidence_units WHERE case_id = :cid "
                "AND result_type NOT IN ('unavailable','blocked','dork_hit','archive_hit') "
                "AND COALESCE(notes, '') NOT LIKE '[candidate-email]%' "
                "ORDER BY timestamp_collected"
            ),
            {"cid": str(case_id)},
        ).mappings().all()
        return [dict(r) for r in rows]

    # ---- 2. build accounts --------------------------------------------------
    def _build_accounts(self, units: list[dict]) -> list[dict]:
        accounts: dict[tuple, dict] = {}
        for u in units:
            platform = (u.get("source_platform") or u.get("tool_name") or "unknown").lower()
            value = u.get("result_value") or ""
            result_type = u.get("result_type") or ""
            profile_url = value if "://" in value else None
            handle_raw = None
            if profile_url:
                handle_raw = unquote(profile_url.rstrip("/").rsplit("/", 1)[-1])
            elif result_type == "account_found" and value and "://" not in value:
                handle_raw = value

            key_part = _clean_username(handle_raw) if handle_raw else (result_type or "acct")
            key = (platform, key_part)
            acc = accounts.get(key)
            if acc is None:
                acc = {
                    "id": f"a{len(accounts)}",
                    "platform": platform,
                    "label": handle_raw or u.get("seed_value") or platform,
                    "url": profile_url,
                    "usernames": set(),
                    "username_display": {},
                    "emails": set(),
                    "phones": set(),
                    "ext_urls": set(),
                    "phashes": set(),
                    "image_embeddings": [],
                    "face_embeddings": [],
                    "bio": None,
                    "tools": set(),
                    "result_types": set(),
                    "first_seen": u.get("timestamp_collected"),
                    "last_seen": u.get("timestamp_collected"),
                }
                accounts[key] = acc

            acc["tools"].add(u.get("tool_name"))
            acc["result_types"].add(u.get("result_type"))
            if profile_url and not acc["url"]:
                acc["url"] = profile_url
            ts = u.get("timestamp_collected")
            if ts:
                acc["first_seen"] = min(acc["first_seen"] or ts, ts)
                acc["last_seen"] = max(acc["last_seen"] or ts, ts)

            self._absorb_identifiers(acc, u, handle_raw)
        return list(accounts.values())

    def _ingest_handle(self, acc: dict, raw: Optional[str]) -> None:
        """Record a handle that may be a username, an email, or a Mastodon
        ``@user@domain`` token. Email-shaped handles are decoded so they bridge
        username clusters to email clusters."""
        token = (raw or "").strip().lstrip("@")
        if not token:
            return
        if "@" in token and (e := _norm_email(token)):
            acc["emails"].add(e)
            token = _email_localpart(e)
        norm = _clean_username(token)
        if norm:
            acc["usernames"].add(norm)
            acc["username_display"].setdefault(norm, _display_username(token))

    def _absorb_identifiers(self, acc: dict, unit: dict, handle_raw: Optional[str]) -> None:
        """Pull every identifier this unit exposes into the account features."""
        seed_type, seed_value = unit.get("seed_type"), unit.get("seed_value")
        if seed_type == "email":
            if (e := _norm_email(seed_value)):
                acc["emails"].add(e)
        elif seed_type == "username":
            # a username seed can itself be email-shaped — decode and split it
            self._ingest_handle(acc, seed_value)
        elif seed_type == "phone":
            if (p := _norm_phone(seed_value)):
                acc["phones"].add(p)

        if unit.get("result_type") == "email_registered" and (e := _norm_email(seed_value)):
            acc["emails"].add(e)

        # A handle that is literally an email (e.g. maigret seeded with an email)
        # bridges username clusters to email clusters — decode and split it.
        self._ingest_handle(acc, handle_raw)

        emb = _parse_vector(unit.get("image_embedding"))
        if emb:
            acc["image_embeddings"].append(emb)
        face = _parse_vector(unit.get("face_embedding"))
        if face:
            acc["face_embeddings"].append(face)

        enrich = unit.get("platform_enrichment")
        if not isinstance(enrich, dict):
            return
        for k in _EMAIL_KEYS:
            if (e := _norm_email(enrich.get(k))):
                acc["emails"].add(e)
        for k in _EMAIL_LIST_KEYS:
            for v in enrich.get(k) or []:
                if (e := _norm_email(v)):
                    acc["emails"].add(e)
        for k in _PHONE_KEYS:
            if (p := _norm_phone(enrich.get(k))):
                acc["phones"].add(p)
        for k in _USERNAME_KEYS:
            self._ingest_handle(acc, str(enrich.get(k) or ""))
        for k in _USERNAME_LIST_KEYS:
            for v in enrich.get(k) or []:
                self._ingest_handle(acc, str(v))
        for k in _URL_KEYS:
            v = enrich.get(k)
            if isinstance(v, str) and "://" in v:
                acc["ext_urls"].add(v.lower())
        for k in _URL_LIST_KEYS:
            for v in enrich.get(k) or []:
                if isinstance(v, str) and "://" in v:
                    acc["ext_urls"].add(v.lower())
        for k in _PHASH_KEYS:
            if enrich.get(k):
                acc["phashes"].add(str(enrich[k]))
        if not acc["bio"]:
            for k in _BIO_KEYS:
                if enrich.get(k):
                    acc["bio"] = str(enrich[k])
                    break

    # ---- 3. pairwise scoring ------------------------------------------------
    def _phash_match(self, a: set, b: set) -> bool:
        if not a or not b:
            return False
        try:
            import imagehash

            for x in a:
                for y in b:
                    if (imagehash.hex_to_hash(x) - imagehash.hex_to_hash(y)) <= PHASH_MAX_DISTANCE:
                        return True
            return False
        except Exception:  # noqa: BLE001
            return bool(a & b)

    def _embedding_match(self, a: list, b: list) -> bool:
        """True when any CLIP avatar embedding pair is the same image (cosine high)."""
        if not a or not b:
            return False
        for x in a:
            for y in b:
                if x and y and len(x) == len(y) and _cosine(x, y) >= IMAGE_SIM_THRESHOLD:
                    return True
        return False

    def _face_match(self, a: list, b: list) -> bool:
        """True when any FaceNet embedding pair is the same person (cosine high)."""
        if not a or not b:
            return False
        for x in a:
            for y in b:
                if x and y and len(x) == len(y) and _cosine(x, y) >= FACE_SIM_THRESHOLD:
                    return True
        return False

    def _score_pair(self, a: dict, b: dict) -> dict:
        reasons: list[dict] = []
        weight = 0

        if (shared := a["emails"] & b["emails"]):
            weight += W_SHARED_EMAIL
            reasons.append({"signal": "shared_email", "detail": sorted(shared)[0]})
        if (shared := a["phones"] & b["phones"]):
            weight += W_SHARED_PHONE
            reasons.append({"signal": "shared_phone", "detail": sorted(shared)[0]})
        if (shared := a["usernames"] & b["usernames"]):
            weight += W_SHARED_USERNAME
            reasons.append({"signal": "shared_username", "detail": sorted(shared)[0]})
        if self._phash_match(a["phashes"], b["phashes"]) or self._embedding_match(
            a["image_embeddings"], b["image_embeddings"]
        ) or self._face_match(a["face_embeddings"], b["face_embeddings"]):
            weight += W_PHOTO_MATCH
            reasons.append({"signal": "photo_match", "detail": "pHash ≤ 8, CLIP or face match"})

        # username on one side equals the email local-part on the other
        bridge = self._handle_email_bridge(a, b) or self._handle_email_bridge(b, a)
        if bridge and not (a["emails"] & b["emails"]):
            weight += W_HANDLE_EMAIL
            reasons.append({"signal": "handle_email_match", "detail": bridge})

        if (shared := a["ext_urls"] & b["ext_urls"]):
            weight += W_SHARED_URL
            reasons.append({"signal": "shared_url", "detail": sorted(shared)[0]})

        # soft signals — only if no exact username already matched
        if not (a["usernames"] & b["usernames"]):
            sim = self._best_username_similarity(a["usernames"], b["usernames"])
            if sim >= USERNAME_SIM_THRESHOLD:
                weight += W_USERNAME_SIM
                reasons.append({"signal": "username_similar", "detail": f"{sim:.0%}"})
        if a["bio"] and b["bio"]:
            ratio = difflib.SequenceMatcher(None, a["bio"], b["bio"]).ratio()
            if ratio >= BIO_SIM_THRESHOLD:
                weight += W_BIO_SIM
                reasons.append({"signal": "bio_similar", "detail": f"{ratio:.0%}"})

        hard = any(r["signal"] in HARD_SIGNALS for r in reasons)
        return {"weight": weight, "reasons": reasons, "hard": hard}

    @staticmethod
    def _handle_email_bridge(src: dict, dst: dict) -> Optional[str]:
        locals_ = {_email_localpart(e) for e in dst["emails"]}
        hit = src["usernames"] & locals_
        return sorted(hit)[0] if hit else None

    @staticmethod
    def _best_username_similarity(set_a: set, set_b: set) -> float:
        best = 0.0
        for x in set_a:
            for y in set_b:
                best = max(best, difflib.SequenceMatcher(None, x, y).ratio())
        return best

    # ---- 4. union-find clustering ------------------------------------------
    def _cluster(self, accounts: list[dict]) -> tuple[list[dict], dict[str, int]]:
        parent = {acc["id"]: acc["id"] for acc in accounts}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x: str, y: str) -> None:
            parent[find(x)] = find(y)

        edges: list[dict] = []
        for i in range(len(accounts)):
            for j in range(i + 1, len(accounts)):
                a, b = accounts[i], accounts[j]
                scored = self._score_pair(a, b)
                if not scored["reasons"]:
                    continue
                merges = scored["hard"] or scored["weight"] >= MERGE_THRESHOLD
                edges.append({
                    "source": a["id"], "target": b["id"],
                    "weight": scored["weight"], "reasons": scored["reasons"],
                    "hard": scored["hard"], "merges": merges,
                })
                if merges:
                    union(a["id"], b["id"])

        root_of = {acc["id"]: find(acc["id"]) for acc in accounts}
        return edges, root_of

    # ---- 5. summarise -------------------------------------------------------
    def resolve(self, case_id: UUID, session: Session) -> dict:
        units = self._load_units(case_id, session)
        accounts = self._build_accounts(units)
        if not accounts:
            return {"case_id": str(case_id), "account_count": 0, "persona_count": 0,
                    "singleton_count": 0, "personas": [], "edges": []}

        edges, root_of = self._cluster(accounts)
        by_id = {acc["id"]: acc for acc in accounts}

        groups: dict[str, list[dict]] = {}
        for acc in accounts:
            groups.setdefault(root_of[acc["id"]], []).append(acc)

        personas = []
        for members in groups.values():
            member_ids = {m["id"] for m in members}
            internal = [e for e in edges
                        if e["merges"] and e["source"] in member_ids and e["target"] in member_ids]
            personas.append(self._summarise(members, internal))

        personas.sort(key=lambda p: (p["account_count"], p["score"]), reverse=True)
        for idx, p in enumerate(personas, start=1):
            p["persona_id"] = f"P{idx}"

        # Alternative hypotheses + per-persona confidence dashboard: strong
        # cross-persona near-miss links that fell just short of merging.
        self._attach_hypotheses(personas, edges, by_id)

        multi = [p for p in personas if p["account_count"] > 1]
        public_edges = self._select_public_edges(edges, by_id)
        return {
            "case_id": str(case_id),
            "account_count": len(accounts),
            "persona_count": len(multi),
            "singleton_count": len(personas) - len(multi),
            "personas": personas,
            "edges": public_edges,
            "edge_count": len(edges),
            "edges_truncated": len(public_edges) < len(edges),
        }

    def _attach_hypotheses(self, personas: list[dict], edges: list[dict], by_id: dict) -> None:
        """Attach cross-persona near-miss links + a confidence dashboard.

        A persona that did NOT merge with another but still shares a meaningful
        signal is a lead the analyst should weigh: confirming it would fuse the
        two clusters. We surface the single strongest such edge per persona
        pair, capped per persona, plus a compact confidence summary.
        """
        acct_to_persona: dict[str, str] = {}
        for p in personas:
            for acc in p["accounts"]:
                acct_to_persona[acc["id"]] = p["persona_id"]

        best: dict[tuple, dict] = {}
        for e in edges:
            if e["merges"]:
                continue
            ps, pt = acct_to_persona.get(e["source"]), acct_to_persona.get(e["target"])
            if not ps or not pt or ps == pt:
                continue
            if not e["hard"] and e["weight"] < _HYPOTHESIS_MIN_WEIGHT:
                continue
            key = tuple(sorted((ps, pt)))
            if key not in best or e["weight"] > best[key]["weight"]:
                best[key] = e

        per_persona: dict[str, list[dict]] = defaultdict(list)
        for (pa, pb), e in best.items():
            shared = {
                "weight": e["weight"],
                "hard": e["hard"],
                "merge_gap": 0.0 if e["hard"] else round(MERGE_THRESHOLD - e["weight"], 1),
                "source_label": by_id[e["source"]]["label"],
                "target_label": by_id[e["target"]]["label"],
                "reasons": [{"signal": r["signal"],
                             "label": _SIGNAL_LABELS.get(r["signal"], r["signal"]),
                             "detail": r["detail"]} for r in e["reasons"]],
            }
            per_persona[pa].append({**shared, "with_persona": pb})
            per_persona[pb].append({**shared, "with_persona": pa})

        for p in personas:
            hyps = sorted(per_persona.get(p["persona_id"], []),
                          key=lambda h: h["weight"], reverse=True)[:_MAX_HYPOTHESES_PER_PERSONA]
            p["alternative_hypotheses"] = hyps
            p["confidence"] = {
                "tier": p["confidence_tier"],
                "score": p["score"],
                "probability": p.get("probability", _calibrate_persona(p["score"])),
                "strongest_signal": p["linking_signals"][0] if p["linking_signals"] else None,
                "signal_diversity": len(p["linking_signals"]),
                "competing_hypotheses": len(hyps),
            }

    def _summarise(self, members: list[dict], internal: list[dict]) -> dict:
        n = len(members)
        platforms = sorted({m["platform"] for m in members})

        disp_map: dict[str, str] = {}
        for m in members:
            disp_map.update(m["username_display"])

        def disp_un(norm: str) -> str:
            return disp_map.get(norm, norm)

        def shared(field: str) -> dict[str, int]:
            counts: dict[str, int] = {}
            for m in members:
                for v in m[field]:
                    counts[v] = counts.get(v, 0) + 1
            return {k: c for k, c in counts.items() if c >= 2}

        shared_emails = shared("emails")
        shared_usernames = shared("usernames")
        shared_phones = shared("phones")

        def spans_all(counts: dict[str, int]) -> Optional[str]:
            return next((k for k, c in counts.items() if c == n), None)

        pivot = (
            (spans_all(shared_emails) and ("email", spans_all(shared_emails)))
            or (spans_all(shared_phones) and ("phone", spans_all(shared_phones)))
            or (spans_all(shared_usernames) and ("username", spans_all(shared_usernames)))
            or (shared_emails and ("email", max(shared_emails, key=shared_emails.get)))
            or (shared_usernames and ("username", max(shared_usernames, key=shared_usernames.get)))
            or None
        )

        weights = [e["weight"] for e in internal]
        avg_w = sum(weights) / len(weights) if weights else 0.0
        hard_frac = (sum(1 for e in internal if e["hard"]) / len(internal)) if internal else 0.0
        has_global_pivot = bool(pivot and pivot[1] in (
            {**shared_emails, **shared_phones, **shared_usernames}
        ) and (
            spans_all(shared_emails) or spans_all(shared_phones) or spans_all(shared_usernames)
        ))

        pivot_out = None
        if pivot:
            pval = disp_un(pivot[1]) if pivot[0] == "username" else pivot[1]
            pivot_out = (pivot[0], pval)

        if n == 1:
            tier, score = "SINGLETON", 0.0
        elif has_global_pivot or avg_w >= 28:
            tier, score = "HIGH", round(min(99.0, 60 + avg_w), 1)
        elif hard_frac == 1.0 or avg_w >= MERGE_THRESHOLD:
            tier, score = "MEDIUM", round(40 + avg_w, 1)
        else:
            tier, score = "LOW", round(20 + avg_w, 1)

        signal_counts: dict[str, int] = {}
        for e in internal:
            for r in e["reasons"]:
                signal_counts[r["signal"]] = signal_counts.get(r["signal"], 0) + 1
        linking = sorted(
            ({"signal": s, "label": _SIGNAL_LABELS.get(s, s), "count": c}
             for s, c in signal_counts.items()),
            key=lambda x: x["count"], reverse=True,
        )

        firsts = [m["first_seen"] for m in members if m["first_seen"]]
        lasts = [m["last_seen"] for m in members if m["last_seen"]]

        return {
            "account_count": n,
            "platform_count": len(platforms),
            "platforms": platforms,
            "confidence_tier": tier,
            "score": score,
            "probability": _calibrate_persona(score),
            "pivot_identifier": ({"kind": pivot_out[0], "value": pivot_out[1]} if pivot_out else None),
            "shared_identifiers": {
                "emails": sorted(shared_emails),
                "usernames": sorted({disp_un(u) for u in shared_usernames}),
                "phones": sorted(shared_phones),
            },
            "linking_signals": linking,
            "accounts": [self._public_account(m) for m in members],
            "timeline": {
                "first_seen": min(firsts).isoformat() if firsts else None,
                "last_seen": max(lasts).isoformat() if lasts else None,
            },
            "explanation": self._explain(n, len(platforms), pivot_out, linking, tier),
        }

    @staticmethod
    def _explain(n, platforms, pivot, linking, tier) -> str:
        if n == 1:
            return "Single account — no cross-platform links found yet."
        if pivot:
            anchor = f"shared {pivot[0]} {pivot[1]}"
        elif linking:
            anchor = linking[0]["label"].lower()
        else:
            anchor = "correlated signals"
        return (f"{n} accounts across {platforms} platforms, linked by {anchor} "
                f"({tier} confidence).")

    @staticmethod
    def _public_account(m: dict) -> dict:
        return {
            "id": m["id"], "platform": m["platform"], "label": m["label"], "url": m["url"],
            "usernames": sorted({m["username_display"].get(u, u) for u in m["usernames"]}),
            "emails": sorted(m["emails"]),
            "phones": sorted(m["phones"]), "tools": sorted(t for t in m["tools"] if t),
            "first_seen": m["first_seen"].isoformat() if m["first_seen"] else None,
        }

    @staticmethod
    def _public_edge(e: dict, by_id: dict) -> dict:
        return {
            "source": e["source"], "target": e["target"], "weight": e["weight"],
            "hard": e["hard"], "merges": e["merges"],
            "source_label": by_id[e["source"]]["label"],
            "target_label": by_id[e["target"]]["label"],
            "reasons": [{"signal": r["signal"],
                         "label": _SIGNAL_LABELS.get(r["signal"], r["signal"]),
                         "detail": r["detail"]} for r in e["reasons"]],
        }

    def _select_public_edges(self, edges: list[dict], by_id: dict) -> list[dict]:
        """Bounded, render-friendly edge subset.

        Merge edges (the cluster backbone) are kept first, then the strongest
        soft edges, capped at ``_MAX_PUBLIC_EDGES``. The full edge set still
        drives clustering and the per-persona signal counts, so capping the
        exported list never changes a persona's score or membership.
        """
        if len(edges) <= _MAX_PUBLIC_EDGES:
            ordered = edges
        else:
            merge = sorted((e for e in edges if e["merges"]),
                           key=lambda e: e["weight"], reverse=True)
            soft = sorted((e for e in edges if not e["merges"]),
                          key=lambda e: e["weight"], reverse=True)
            ordered = (merge + soft)[:_MAX_PUBLIC_EDGES]
        return [self._public_edge(e, by_id) for e in ordered]
