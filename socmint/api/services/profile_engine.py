"""MODULE 6c — Subject Profiling Engine ("the deep brain").

Where the correlation engine links accounts and the insight engine scores
exposure, this engine answers the question a real investigator actually asks:

    "Who is this person?"

It fuses every ``platform_enrichment`` blob, identifier, timestamp and handle a
case has gathered into a single, explainable **subject dossier**:

  * inferred real-world *attributes* (candidate names, locations, languages,
    occupation/affiliations, online tenure) — each with a confidence and the
    evidence that backs it,
  * a *behavioral fingerprint* (handle-naming style, avatar reuse, bio
    consistency, a cross-platform consistency score),
  * *temporal* analysis (first/last activity, active span, a creation timeline),
  * an *interest / sophistication* read from the platform mix,
  * a *digital-footprint* score, and
  * human-readable *reasoning chains* plus a narrative dossier summary.

Design: the core (:meth:`ProfileEngine.build`) is **pure** — plain dict inputs,
plain dict output, no DB / network / framework objects — so it is trivially
unit-testable with DTO fixtures and reusable from both the API router and the
report generator.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from urllib.parse import urlparse

from api.services.insight_engine import _host_to_platform, _platform_of

# --- result_type taxonomy ----------------------------------------------------
NULL_TYPES = {"unavailable", "blocked"}
PROFILE_TYPES = {"account_found"}

# Candidate (derived/guessed) identifiers are persisted with this notes sentinel
# so they surface as unconfirmed leads, never as confirmed identity. Mirrors
# CANDIDATE_NOTE_PREFIX in worker_python/tasks/tier2_tasks.py.
CANDIDATE_NOTE_PREFIX = "[candidate-email]"


def _is_candidate(e: dict) -> bool:
    """True for evidence flagged as a derived/unconfirmed candidate lead."""
    return (e.get("notes") or "").startswith(CANDIDATE_NOTE_PREFIX)

# --- enrichment key groups (lower-cased lookup) ------------------------------
NAME_KEYS = ("name", "full_name", "fullname", "display_name", "real_name", "realname")
LOCATION_KEYS = (
    "location", "geo", "city", "country", "region", "place", "address",
    "geo_description", "circle", "hometown",
)
LANG_KEYS = ("language", "lang", "locale")
COMPANY_KEYS = ("company", "organization", "organisation", "org", "employer")
HEADLINE_KEYS = ("headline", "occupation", "job_title", "jobtitle", "position", "role")
BIO_KEYS = ("bio", "description", "about", "summary", "biography")
WEBSITE_KEYS = ("blog", "website", "url", "external_url", "homepage", "site")
AVATAR_KEYS = (
    "avatar_url", "profile_pic_url", "profile_image", "picture", "image",
    "avatar", "photo", "profile_picture", "thumbnail",
)
CREATED_KEYS = (
    "created_at", "join_date", "joined", "joined_at", "registered", "created",
    "date_joined", "member_since", "creation_date",
)
PHASH_KEYS = ("phash", "profile_pic_hash")
EMAIL_KEYS = ("email", "public_email")
EMAIL_LIST_KEYS = ("emails", "discovered_emails")
PHONE_KEYS = ("phone", "phone_number", "e164")
# Numbers some platforms expose under their own keys. toutatis (Instagram) emits
# a *masked* "obfuscated phone" plus, when the owner made it public, a full
# "public phone" — both keys contain a space. Full numbers become real contact
# numbers; masked ones are kept verbatim as low-confidence investigative leads.
PUBLIC_PHONE_KEYS = ("public phone", "public_phone")
OBFUSCATED_PHONE_KEYS = ("obfuscated phone", "obfuscated_phone")
USERNAME_KEYS = (
    "username", "login", "handle", "screen_name", "twitter_username",
    "telegram_username", "nick", "nickname",
)
USERNAME_LIST_KEYS = ("discovered_usernames",)
TIMEZONE_KEYS = ("timezone", "tz", "time_zone")
VERIFIED_KEYS = ("is_verified", "verified")

# --- platform → interest category --------------------------------------------
PLATFORM_CATEGORY = {
    # developer / technical
    "github.com": "developer", "gitlab.com": "developer", "bitbucket.org": "developer",
    "stackoverflow.com": "developer", "stackexchange.com": "developer",
    "news.ycombinator.com": "developer", "hackernews": "developer",
    "npmjs.com": "developer", "pypi.org": "developer", "hub.docker.com": "developer",
    "keybase.io": "developer", "replit.com": "developer", "codepen.io": "developer",
    "kaggle.com": "developer", "leetcode.com": "developer", "codeforces.com": "developer",
    "hackerone.com": "developer", "devto": "developer", "dev.to": "developer",
    "archlinux.org": "developer", "gitlab.archlinux.org": "developer", "habr.com": "developer",
    "qna.habr.com": "developer", "freecodecamp.org": "developer", "hackthebox.com": "developer",
    "hackthebox.eu": "developer", "tryhackme.com": "developer", "247ctf.com": "developer",
    "exploit-db.com": "developer", "wordpress.org": "developer", "atlassian.com": "developer",
    "sourceforge.net": "developer",
    # professional
    "linkedin.com": "professional", "xing.com": "professional",
    "angel.co": "professional", "crunchbase.com": "professional",
    # social
    "instagram.com": "social", "facebook.com": "social", "twitter.com": "social",
    "x.com": "social", "tiktok.com": "social", "snapchat.com": "social",
    "threads.net": "social", "mastodon.social": "social", "bsky.app": "social",
    "vk.com": "social", "weibo.com": "social", "telegram.org": "social", "t.me": "social",
    "zhihu.com": "social", "disqus.com": "social", "reddit.com": "social",
    "quora.com": "social", "ok.ru": "social", "tellonym.me": "social",
    "periscope.tv": "social", "gravatar.com": "social", "about.me": "social",
    # gaming
    "steamcommunity.com": "gaming", "twitch.tv": "gaming", "discord.com": "gaming",
    "xbox.com": "gaming", "roblox.com": "gaming", "chess.com": "gaming",
    "lichess.org": "gaming", "speedrun.com": "gaming",
    "gog.com": "gaming", "xboxgamertag.com": "gaming", "habbo.com": "gaming",
    "pokemonshowdown.com": "gaming", "epicgames.com": "gaming", "playstation.com": "gaming",
    "osu.ppy.sh": "gaming",
    # creative / media
    "youtube.com": "creative", "vimeo.com": "creative", "soundcloud.com": "creative",
    "spotify.com": "creative", "behance.net": "creative", "dribbble.com": "creative",
    "deviantart.com": "creative", "flickr.com": "creative", "500px.com": "creative",
    "pinterest.com": "creative", "medium.com": "creative", "wordpress.com": "creative",
    "tumblr.com": "creative", "blogger.com": "creative", "substack.com": "creative",
    "patreon.com": "creative",
    "teletype.in": "creative", "wattpad.com": "creative", "newgrounds.com": "creative",
    "bandcamp.com": "creative", "mixcloud.com": "creative", "last.fm": "creative",
    "imgur.com": "creative", "giphy.com": "creative", "slideshare.net": "creative",
    # --- extended coverage (keys are registrable domains; subdomains collapse here) ---
    # developer / technical
    "docker.com": "developer", "codeberg.org": "developer", "gitea.com": "developer",
    "gitea.io": "developer", "codechef.com": "developer", "hackerrank.com": "developer",
    "greasyfork.org": "developer", "hackaday.io": "developer", "huggingface.co": "developer",
    "rubygems.org": "developer", "devrant.com": "developer", "arduino.cc": "developer",
    "pastebin.com": "developer", "mit.edu": "developer",
    # gaming
    "dota2.ru": "gaming", "truckersmp.com": "gaming", "gamejolt.com": "gaming",
    "gdbrowser.com": "gaming", "tetr.io": "gaming", "jeuxvideo.com": "gaming",
    "mmorpg.com": "gaming", "steamgifts.com": "gaming", "twitchtracker.com": "gaming",
    "kick.com": "gaming", "playerdb.co": "gaming", "mcuuid.net": "gaming",
    "habbo.de": "gaming", "habbo.es": "gaming", "habbo.it": "gaming",
    # creative / media
    "audiojungle.net": "creative", "bandlab.com": "creative", "freesound.org": "creative",
    "themeforest.net": "creative", "redbubble.com": "creative", "letterboxd.com": "creative",
    "scribd.com": "creative", "archiveofourown.org": "creative", "livejournal.com": "creative",
    "flipboard.com": "creative", "imageshack.com": "creative", "neocities.org": "creative",
    "donationalerts.com": "creative", "filmweb.pl": "creative", "fansly.com": "creative",
    # social
    "wykop.pl": "social", "kwejk.pl": "social", "jbzd.com.pl": "social",
    "zbiornik.com": "social", "kik.me": "social", "plurk.com": "social",
    "myspace.com": "social", "untappd.com": "social", "chatango.com": "social",
    "postcrossing.com": "social", "dating.ru": "social", "drive2.ru": "social",
    "palnet.io": "social",
    # professional
    "freelancer.com": "professional", "fl.ru": "professional", "seoclerks.com": "professional",
}
_CATEGORY_LABEL = {
    "developer": "Software / technical",
    "professional": "Professional networking",
    "social": "Social media",
    "gaming": "Gaming",
    "creative": "Creative / media / publishing",
    "other": "Other",
}

# --- light heuristics --------------------------------------------------------
_OCCUPATION_HINTS = {
    "engineer": "Engineering", "developer": "Software development",
    "programmer": "Software development", "founder": "Founder / entrepreneur",
    "ceo": "Executive", "cto": "Executive", "designer": "Design",
    "student": "Student", "researcher": "Research", "scientist": "Research",
    "manager": "Management", "consultant": "Consulting", "analyst": "Analysis",
    "journalist": "Media / journalism", "writer": "Writing", "artist": "Arts",
    "photographer": "Photography", "musician": "Music", "teacher": "Education",
    "professor": "Academia", "lawyer": "Legal", "doctor": "Medicine",
    "nurse": "Healthcare", "marketer": "Marketing", "trader": "Finance",
    "hacker": "Security / hacking", "pentester": "Security", "gamer": "Gaming",
    "streamer": "Streaming / content",
}
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_LEET_CHARS = set("01345789@$")

# Common public email providers — used to detect when a seed email has been
# embedded as a SUBDOMAIN of another host (e.g. "user@gmail.com.wordpress.com",
# produced by username-enumeration tools that template the seed into a URL).
# Such strings are not real email addresses and must not pollute the dossier.
_EMAIL_PROVIDERS = (
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com",
    "outlook.com", "live.com", "icloud.com", "me.com", "proton.me",
    "protonmail.com", "aol.com", "gmx.com", "mail.com", "zoho.com",
    "yandex.com", "rediffmail.com",
)

# Domain-suffix tokens that reveal a "username" is really a host/domain artifact
# (a leaked subdomain split, not a handle), e.g. "gmail.com.bsky.social".
_TLD_NOISE = (
    ".com", ".net", ".org", ".io", ".co", ".social", ".cz", ".in", ".me",
    ".dev", ".app", ".xyz", ".info", ".gov", ".edu",
)


def _clean_email(value: str) -> str | None:
    """Return a normalised email, or None if it is not a genuine address.

    Rejects the common false positive where a seed email has been concatenated
    into a hostname (``user@gmail.com.wordpress.com``): when a known provider
    domain appears mid-string followed by more labels, it is a templated URL
    artifact, not an address.
    """
    if not value:
        return None
    email = value.strip().lower().strip(".")
    m = _EMAIL_RE.fullmatch(email)
    if not m:
        return None
    local, _, domain = email.partition("@")
    if not local or domain.count(".") < 1:
        return None
    # Provider domain embedded as a subdomain -> templated host, not an email.
    for prov in _EMAIL_PROVIDERS:
        if domain != prov and domain.startswith(prov + "."):
            return None
    return email


def _looks_like_domain(value: str) -> bool:
    """True when a candidate handle is really a host/domain fragment."""
    low = (value or "").lower()
    if "." in low and any(low.endswith(t) or (t + ".") in low or low.startswith(t.lstrip(".") + ".") for t in _TLD_NOISE):
        return True
    return any((t + ".") in ("." + low) for t in _TLD_NOISE)

# URL path/query tokens that are API endpoints or pages, never real handles.
_API_NOISE = {
    "api", "public", "users", "user", "search", "details", "profile", "profiles",
    "profil", "autocomplete", "advancedsearch", "getprofile", "publications", "actor",
    "people", "member", "members", "lookup", "query", "find", "account",
    "accounts", "v1", "v2", "v3", "v4", "graphql", "rest", "json", "oauth",
    "auth", "about", "home", "index", "explore", "results", "page", "settings",
    "site", "info", "id", "www", "web", "login", "signup", "register", "new",
    "commands", "command", "help", "docs", "doc", "status", "feed", "tag", "tags",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value) -> datetime | None:
    """Best-effort parse of an ISO-ish timestamp into an aware datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    # epoch seconds?
    if re.fullmatch(r"\d{9,13}", text):
        try:
            ts = int(text)
            if ts > 1e12:  # milliseconds
                ts /= 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    cleaned = text.replace("Z", "+00:00")
    for candidate in (cleaned, cleaned[:19], cleaned[:10]):
        try:
            dt = datetime.fromisoformat(candidate)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    m = re.search(r"(19|20)\d{2}", text)  # last resort: a bare year
    if m:
        try:
            return datetime(int(m.group(0)), 1, 1, tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _first(enrich: dict, keys: tuple[str, ...]):
    """Return the first present, non-empty value among ``keys`` (case-insensitive)."""
    if not isinstance(enrich, dict):
        return None
    lower = {str(k).lower(): v for k, v in enrich.items()}
    for k in keys:
        v = lower.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _looks_leet(handle: str) -> bool:
    """A handle 'looks leet' if a digit/symbol sits between two letters (e.g. h4ck3r)."""
    h = handle or ""
    for i in range(1, len(h) - 1):
        if h[i] in _LEET_CHARS and h[i - 1].isalpha() and h[i + 1].isalpha():
            return True
    return False


def _stem(handle: str) -> str:
    """Alphabetic stem of a handle (strip separators + trailing/leading digits)."""
    base = re.sub(r"[._\-]+", "", (handle or "").lower())
    base = re.sub(r"\d+$", "", base)
    base = re.sub(r"^\d+", "", base)
    return base


# Platforms whose presence is itself a behavioural signal (inferred leads only).
_SECURITY_PLATFORMS = {
    "hackthebox.com", "hackthebox.eu", "tryhackme.com", "247ctf.com",
    "exploit-db.com", "hackerone.com",
}
_PRIVACY_PLATFORMS = {
    "protonmail.com", "proton.me", "mastodon.social", "telegram.org", "t.me",
    "keybase.io", "tutanota.com",
}


class ProfileEngine:
    """Synthesise raw evidence into an explainable subject dossier."""

    # ------------------------------------------------------------------- API
    def build(
        self,
        evidence: list[dict],
        links: list[dict] | None = None,
        case: dict | None = None,
        persona: dict | None = None,
        include_ai: bool = False,
    ) -> dict:
        """Return the full subject dossier for one case (pure function).

        ``include_ai`` controls the optional local-LLM dossier summary: OFF by
        default so the interactive dossier endpoint stays fast, ON for the report
        generator and the dedicated ai-summary endpoint.
        """
        links = links or []
        case = case or {}
        live = [e for e in evidence if e.get("result_type") not in NULL_TYPES]

        feats = self._collect(live, case)
        identity = self._identity(feats)
        attributes = self._attributes(feats, identity)
        behavior = self._behavior(feats)
        temporal = self._temporal(feats)
        interests = self._interests(feats)
        footprint = self._footprint(feats, attributes)
        behavioral_indicators = self._behavioral_indicators(
            feats, behavior, interests, temporal, footprint, attributes
        )
        reasoning = self._reasoning(identity, attributes, behavior, temporal, interests, footprint)
        completeness = self._completeness(identity, attributes, behavior)
        summary = self._summary(identity, attributes, behavior, temporal, interests, footprint, case)
        ai_summary = self._ai_summary(
            identity, attributes, behavior, temporal, interests, footprint, case, summary
        ) if include_ai else None

        # SDM behavioral data — extracted from enrichment blobs
        sdm_behavioral, sdm_network, sdm_communities = self._sdm_data(live)

        return {
            "generated_at": _now().isoformat(),
            "case_id": str(case.get("case_id", "")),
            "identity": identity,
            "attributes": attributes,
            "behavioral_fingerprint": behavior,
            "behavioral_indicators": behavioral_indicators,
            "temporal": temporal,
            "interests": interests,
            "footprint": footprint,
            "reasoning": reasoning,
            "profile_completeness": completeness,
            "persona_count": (persona or {}).get("persona_count"),
            "summary": summary,
            "ai_summary": ai_summary,
            "sdm_behavioral": sdm_behavioral,
            "sdm_network": sdm_network,
            "sdm_communities": sdm_communities,
        }

    # -------------------------------------------------------------- collection
    def _collect(self, live: list[dict], case: dict) -> dict:
        """Walk every evidence unit once, harvesting raw signals into buckets."""
        f = {
            "names": Counter(),
            "name_sources": defaultdict(set),
            "locations": Counter(),
            "location_sources": defaultdict(set),
            "languages": Counter(),
            "companies": Counter(),
            "headlines": Counter(),
            "bios": [],          # list of (platform, text)
            "websites": Counter(),
            "avatars": set(),
            "phashes": Counter(),
            "timezones": Counter(),
            "emails": Counter(),
            "candidate_emails": Counter(),       # derived/guessed -> #registrations
            "candidate_email_platforms": defaultdict(set),
            "phones": Counter(),          # full numbers -> #observations
            "phones_masked": Counter(),   # masked/partial numbers (kept verbatim)
            "phone_sources": defaultdict(set),  # phone value -> {platform/tool}
            "usernames": Counter(),
            "username_sources": defaultdict(set),
            "seed_usernames": set(),
            "account_handles": Counter(),   # clean per-account handle -> #accounts
            "verified_platforms": set(),
            "created": [],       # list of (platform, datetime)
            "first_seen": [],    # list of (platform, datetime)
            "activity_times": [],   # subject-activity datetimes (creations + EXIF)
            "geo_coordinates": [],  # [{lat, lon, platform, captured_at, camera}]
            "accounts": [],      # confirmed/profile accounts: {platform, handle, tier, tools}
            "platform_tiers": defaultdict(lambda: 99),
            "platform_tools": defaultdict(set),
        }

        # seed identifiers
        for seed in (
            case.get("seed_value"),
            *[e.get("seed_value") for e in live if not _is_candidate(e)],
        ):
            self._bucket_seed(str(seed or ""), f)

        accounts: dict[tuple, dict] = {}
        for e in live:
            tool = e.get("tool_name") or "?"
            platform = _platform_of(e)
            rt = e.get("result_type")
            tier = e.get("source_tier")
            tier = int(tier) if tier is not None else 2
            val = e.get("result_value") or ""

            # Derived/guessed candidate identifiers are unconfirmed leads: record
            # them in a separate bucket and skip all confirmed-identity harvesting.
            if _is_candidate(e):
                cem = (e.get("seed_value") or "").strip().lower()
                if cem:
                    f["candidate_emails"][cem] += 1
                    if platform and platform not in ("unknown", ""):
                        f["candidate_email_platforms"][cem].add(platform)
                continue

            # emails from any result value (e.g. theHarvester). Skip URLs — a
            # templated host like "user@gmail.com.wordpress.com" is not an email.
            if not val.startswith("http"):
                for m in _EMAIL_RE.findall(val):
                    if (em := _clean_email(m)):
                        f["emails"][em] += 1

            # phone numbers surfaced directly as a result value:
            #   email2whatsapp -> "whatsapp_hit" (a derived full candidate number)
            #   phoneinfoga    -> "phone_intel"  (a validated E.164 number)
            if rt in ("whatsapp_hit", "phone_intel"):
                self._add_phone(val, platform or tool, f)

            ts = _parse_dt(e.get("timestamp_collected"))
            if ts:
                f["first_seen"].append((platform, ts))

            if rt == "account_found" and platform not in ("unknown", ""):
                key = (platform,)
                acc = accounts.setdefault(key, {
                    "platform": platform, "handle": "", "tools": set(),
                    "tier": tier, "url": val if val.startswith("http") else "",
                })
                acc["tools"].add(tool)
                acc["tier"] = min(acc["tier"], tier)
                f["platform_tiers"][platform] = min(f["platform_tiers"][platform], tier)
                f["platform_tools"][platform].add(tool)

            self._harvest_enrichment(e, platform, tier, tool, f)

        # finalise account handles + username corpus from the picked handles
        for acc in accounts.values():
            handle = self._handle_of(acc, f)
            acc["handle"] = handle
            acc["tools"] = sorted(t for t in acc["tools"] if t)
            if handle and not _looks_like_domain(handle):
                low = handle.lower()
                f["account_handles"][low] += 1
                f["usernames"][low] += 1
                f["username_sources"][low].add(acc["platform"])
            f["accounts"].append(acc)
        return f

    def _bucket_seed(self, raw: str, f: dict) -> None:
        raw = raw.strip()
        if not raw:
            return
        if _EMAIL_RE.fullmatch(raw):
            if (em := _clean_email(raw)):
                f["emails"][em] += 1
        elif raw.startswith("+") or (raw.replace(" ", "").replace("-", "").isdigit() and len(raw) >= 7):
            f["phones"][re.sub(r"[\s\-]", "", raw)] += 1
        elif not raw.startswith("http") and not _looks_like_domain(raw):
            f["usernames"][raw.lower()] += 1
            f["username_sources"][raw.lower()].add("seed")
            f["seed_usernames"].add(raw.lower())

    def _harvest_enrichment(self, e: dict, platform: str, tier: int, tool: str, f: dict) -> None:
        enrich = e.get("platform_enrichment")
        if not isinstance(enrich, dict):
            return

        if (name := _first(enrich, NAME_KEYS)) and isinstance(name, str):
            name = name.strip()
            if 2 <= len(name) <= 60 and not name.startswith("http"):
                f["names"][name] += 1
                f["name_sources"][name].add(tool)
        loc_val = _first(enrich, LOCATION_KEYS)
        if isinstance(loc_val, str) and loc_val.strip():
            loc = loc_val.strip()
            f["locations"][loc] += 1
            f["location_sources"][loc].add(tool)
        if (lang := _first(enrich, LANG_KEYS)) and isinstance(lang, str):
            f["languages"][lang.strip()] += 1
        if (co := _first(enrich, COMPANY_KEYS)) and isinstance(co, str) and co.strip():
            f["companies"][co.strip()] += 1
        if (hl := _first(enrich, HEADLINE_KEYS)) and isinstance(hl, str) and hl.strip():
            f["headlines"][hl.strip()] += 1
        if (bio := _first(enrich, BIO_KEYS)) and isinstance(bio, str) and bio.strip():
            f["bios"].append((platform, bio.strip()))
        if (site := _first(enrich, WEBSITE_KEYS)) and isinstance(site, str) and "." in site:
            f["websites"][site.strip().lower()] += 1
        if (av := _first(enrich, AVATAR_KEYS)) and isinstance(av, str) and av.startswith("http"):
            f["avatars"].add(av)
        for k in PHASH_KEYS:
            if enrich.get(k):
                f["phashes"][str(enrich[k])] += 1
        if (tz := _first(enrich, TIMEZONE_KEYS)) and isinstance(tz, str):
            f["timezones"][tz.strip()] += 1
        for k in EMAIL_KEYS:
            if isinstance(enrich.get(k), str) and (m := _EMAIL_RE.search(enrich[k])):
                if (em := _clean_email(m.group(0))):
                    f["emails"][em] += 1
        for k in EMAIL_LIST_KEYS:
            for v in enrich.get(k) or []:
                if isinstance(v, str) and (m := _EMAIL_RE.search(v)):
                    if (em := _clean_email(m.group(0))):
                        f["emails"][em] += 1
        for k in PHONE_KEYS + PUBLIC_PHONE_KEYS:
            if enrich.get(k):
                self._add_phone(enrich[k], tool or platform, f)
        for k in OBFUSCATED_PHONE_KEYS:
            if enrich.get(k):
                masked_val = str(enrich[k]).strip()
                if masked_val and any(ch.isdigit() for ch in masked_val):
                    f["phones_masked"][masked_val] += 1
                    f["phone_sources"][masked_val].add(tool or platform)
        for k in USERNAME_KEYS:
            v = enrich.get(k)
            if isinstance(v, str) and v.strip() and "@" not in v and not _looks_like_domain(v):
                f["usernames"][v.strip().lower()] += 1
                f["username_sources"][v.strip().lower()].add(tool)
        for k in USERNAME_LIST_KEYS:
            for v in enrich.get(k) or []:
                if isinstance(v, str) and v.strip() and not _looks_like_domain(v):
                    f["usernames"][v.strip().lower()] += 1
                    f["username_sources"][v.strip().lower()].add(tool)
        for k in VERIFIED_KEYS:
            if enrich.get(k) is True:
                f["verified_platforms"].add(platform)
        if (created := _first(enrich, CREATED_KEYS)) and (dt := _parse_dt(created)):
            f["created"].append((platform, dt))
            f["activity_times"].append(dt)

        # EXIF geolocation/camera/capture-time from a downloaded image (photo_hash).
        exif = enrich.get("exif")
        if isinstance(exif, dict):
            gps = exif.get("gps")
            if isinstance(gps, dict) and gps.get("lat") is not None and gps.get("lon") is not None:
                f["geo_coordinates"].append({
                    "lat": gps["lat"],
                    "lon": gps["lon"],
                    "platform": platform,
                    "captured_at": exif.get("captured_at"),
                    "camera": exif.get("camera"),
                })
            if (cap := _parse_dt(exif.get("captured_at"))):
                f["activity_times"].append(cap)

    def _handle_of(self, acc: dict, f: dict) -> str:
        """Pick a clean display handle for an account from its URL.

        Strongly prefers a known seed username when it appears anywhere in the
        URL (path segment, ``@handle``, or a ``?name=`` style query), because
        username-enumeration tools confirm *the same handle* on many sites via
        wildly different URL shapes (``/u/x``, ``/api/public/users?name=x``).
        Falls back to structured ``/users/<h>`` patterns, then a clean last path
        segment — rejecting API/page tokens and anything containing a dot.
        """
        url = acc.get("url") or ""
        if not url:
            return ""
        low = url.lower()

        # 1. a known seed handle present anywhere in the URL wins outright.
        for seed in f["seed_usernames"]:
            if seed and re.search(rf"(?:^|[/@=:_]){re.escape(seed)}(?:$|[/?&=._-]|$)", low):
                return seed

        # 2. structured "this is the user" URL patterns.
        for pat in (
            r"/users?/([A-Za-z0-9_\-]{2,30})",
            r"/u/([A-Za-z0-9_\-]{2,30})",
            r"/@([A-Za-z0-9_\-]{2,30})",
            r"[?&](?:user|username|name|q|u|screen_name)=([A-Za-z0-9_\-]{2,30})",
        ):
            m = re.search(pat, url)
            if m and m.group(1).lower() not in _API_NOISE:
                return m.group(1)

        # 3. a clean last path segment (no dots, not an API/page token).
        seg = urlparse(url).path.strip("/").split("/")[-1].lstrip("@") if url.startswith("http") else ""
        if re.fullmatch(r"[A-Za-z0-9_\-]{2,30}", seg) and seg.lower() not in _API_NOISE and not _looks_like_domain(seg):
            return seg
        return ""

    # ----------------------------------------------------------------- identity
    def _identity(self, f: dict) -> dict:
        def ranked(counter: Counter, sources: dict | None = None) -> list[dict]:
            out = []
            for value, freq in counter.most_common():
                entry = {"value": value, "observations": freq}
                if sources is not None:
                    entry["sources"] = sorted(s for s in sources.get(value, set()) if s)
                out.append(entry)
            return out

        return {
            "emails": ranked(f["emails"]),
            "phones": ranked(f["phones"]),
            "usernames": ranked(f["usernames"], f["username_sources"]),
            "verified_on": sorted(f["verified_platforms"]),
        }

    # --------------------------------------------------------------- attributes
    def _attributes(self, f: dict, identity: dict) -> dict:
        """Inferred real-world attributes, each with a confidence + evidence basis."""
        attrs: dict = {}

        # candidate real names — drop entries that are merely a known handle in
        # stylised casing (e.g. "ToRvaLDs"), which some extractors echo as a name.
        def _norm(s: str) -> str:
            return re.sub(r"[\s._\-]", "", s.lower())

        handle_norms = {_norm(u["value"]) for u in identity.get("usernames", []) if u.get("value")}
        handle_norms |= {_norm(h) for h in f.get("seed_usernames", set())}
        names = []
        for name, freq in f["names"].most_common(5):
            if _norm(name) in handle_norms:
                continue
            src = sorted(s for s in f["name_sources"].get(name, set()) if s)
            names.append({
                "value": name,
                "confidence": self._conf(freq, len(src)),
                "observations": freq,
                "sources": src,
            })
        attrs["names"] = names

        # geolocation: explicit locations + timezone + phone country
        locations = []
        for loc, freq in f["locations"].most_common(5):
            if not loc:
                continue
            src = sorted(s for s in f["location_sources"].get(loc, set()) if s)
            locations.append({
                "value": loc,
                "confidence": self._conf(freq, len(src)),
                "observations": freq,
                "sources": src,
            })
        tz_hint = f["timezones"].most_common(1)[0][0] if f["timezones"] else None
        phone_cc = self._phone_country(identity["phones"])
        attrs["locations"] = locations
        attrs["timezone"] = tz_hint
        attrs["phone_region"] = phone_cc
        attrs["contact_numbers"] = self._contact_numbers(f)

        # languages
        attrs["languages"] = [
            {"value": lang, "observations": freq}
            for lang, freq in f["languages"].most_common(5) if lang
        ]

        # candidate identifiers — derived (e.g. username@provider guesses) and
        # NOT confirmed to belong to the subject; surfaced as low-confidence leads.
        candidate_emails = []
        for email, freq in f["candidate_emails"].most_common(10):
            plats = sorted(p for p in f["candidate_email_platforms"].get(email, set()) if p)
            candidate_emails.append({
                "value": email,
                "observations": freq,
                "platforms": plats,
                "confidence": 0.35,
                "note": "derived from username; ownership unconfirmed",
            })
        attrs["candidate_emails"] = candidate_emails

        # occupation / affiliation
        occ = self._occupation(f)
        attrs["occupation"] = occ["roles"]
        attrs["affiliations"] = [
            {"value": c, "observations": n} for c, n in f["companies"].most_common(5)
        ]
        attrs["headlines"] = [h for h, _ in f["headlines"].most_common(3)]

        # online presence text
        attrs["bios"] = [{"platform": p, "text": t} for p, t in f["bios"][:8]]
        attrs["websites"] = [w for w, _ in f["websites"].most_common(8)]
        attrs["avatar_urls"] = sorted(f["avatars"])[:8]
        attrs["geolocations"] = self._geolocations(f)
        return attrs

    @staticmethod
    def _geolocations(f: dict) -> list[dict]:
        """De-duplicated EXIF GPS fixes recovered from collected images.

        A geotag is a hard, high-confidence physical-location lead — the only
        coordinate-level signal in the dossier — so it is surfaced distinctly
        from soft, text-derived ``locations``.
        """
        seen: dict[tuple, dict] = {}
        for g in f.get("geo_coordinates", []):
            key = (round(g["lat"], 4), round(g["lon"], 4))
            entry = seen.setdefault(key, {
                "lat": g["lat"], "lon": g["lon"],
                "platforms": set(), "captured_at": g.get("captured_at"),
                "camera": g.get("camera"), "observations": 0,
            })
            entry["observations"] += 1
            if g.get("platform"):
                entry["platforms"].add(g["platform"])
            entry["captured_at"] = entry["captured_at"] or g.get("captured_at")
            entry["camera"] = entry["camera"] or g.get("camera")
        out = []
        for e in seen.values():
            e["platforms"] = sorted(e["platforms"])
            e["maps_url"] = f"https://www.openstreetmap.org/?mlat={e['lat']}&mlon={e['lon']}#map=15/{e['lat']}/{e['lon']}"
            out.append(e)
        return out

    @staticmethod
    def _conf(freq: int, n_sources: int) -> float:
        """Confidence 0–1 from how often and how broadly a value was observed."""
        base = 0.35 + 0.18 * math.log2(freq + 1) + 0.12 * max(0, n_sources - 1)
        return round(min(0.97, base), 2)

    @staticmethod
    def _add_phone(raw, source: str, f: dict) -> None:
        """Record a discovered phone number with provenance.

        A fully-numeric number (after stripping +, spaces, dashes, brackets and
        dots) is normalised to ``+<digits>`` and treated as a real contact
        number. Anything still carrying a mask glyph or letters (e.g. an
        Instagram-style ``+91 ****1234``) is kept verbatim as a partial lead.
        """
        if raw is None:
            return
        s = str(raw).strip()
        if not s:
            return
        core = re.sub(r"[\s\-\(\)\.\+]", "", s)
        if not core.isdigit():
            if any(ch.isdigit() for ch in s):
                f["phones_masked"][s] += 1
                f["phone_sources"][s].add(source)
            return
        if 7 <= len(core) <= 15:
            ph = f"+{core}"
            f["phones"][ph] += 1
            f["phone_sources"][ph].add(source)

    def _contact_numbers(self, f: dict) -> list[dict]:
        """Phone numbers recovered from the subject's own linked accounts.

        Full numbers are graded by corroboration; masked numbers are reported
        as low-confidence partial leads (capped at 0.4) so an analyst can pursue
        them through lawful process without over-trusting a fragment.
        """
        out: list[dict] = []
        for ph, freq in f["phones"].most_common(10):
            src = sorted(s for s in f["phone_sources"].get(ph, set()) if s)
            out.append({
                "value": ph,
                "obfuscated": False,
                "confidence": self._conf(freq, len(src)),
                "observations": freq,
                "sources": src,
                "region": self._phone_country([{"value": ph}]),
            })
        for ph, freq in f["phones_masked"].most_common(10):
            src = sorted(s for s in f["phone_sources"].get(ph, set()) if s)
            out.append({
                "value": ph,
                "obfuscated": True,
                "confidence": round(min(0.4, self._conf(freq, len(src))), 2),
                "observations": freq,
                "sources": src,
                "region": None,
            })
        return out

    @staticmethod
    def _phone_country(phones: list[dict]) -> str | None:
        """Map a leading country code to a coarse region label (best effort)."""
        cc_map = {
            "1": "North America (US/Canada)", "44": "United Kingdom", "91": "India",
            "61": "Australia", "49": "Germany", "33": "France", "7": "Russia/Kazakhstan",
            "81": "Japan", "86": "China", "55": "Brazil", "971": "UAE", "92": "Pakistan",
            "880": "Bangladesh", "234": "Nigeria", "27": "South Africa", "39": "Italy",
            "34": "Spain", "82": "South Korea", "65": "Singapore", "60": "Malaysia",
        }
        for p in phones:
            digits = re.sub(r"[^0-9]", "", p.get("value", ""))
            for length in (3, 2, 1):
                if digits[:length] in cc_map:
                    return cc_map[digits[:length]]
        return None

    def _occupation(self, f: dict) -> dict:
        text = " ".join([h.lower() for h in f["headlines"]] + [b.lower() for _, b in f["bios"]])
        roles = []
        for hint, label in _OCCUPATION_HINTS.items():
            if re.search(rf"\b{re.escape(hint)}", text):
                roles.append(label)
        # de-dupe preserving order
        seen, uniq = set(), []
        for r in roles:
            if r not in seen:
                seen.add(r)
                uniq.append(r)
        return {"roles": uniq[:5]}

    # -------------------------------------------------------------- behavioral
    def _behavior(self, f: dict) -> dict:
        # Per-account clean handles (handle -> number of accounts using it).
        acc_handles: Counter = f["account_handles"]
        # Fall back to the discovered-username corpus only if no accounts had
        # a resolvable handle (keeps single-platform / enrichment-only cases working).
        if not acc_handles:
            acc_handles = Counter({h: c for h, c in f["usernames"].items()})

        distinct = list(acc_handles.keys())
        n_distinct = len(distinct)
        total_accounts = sum(acc_handles.values()) or 1

        # Consistency is weighted by *accounts*: what fraction of all confirmed
        # accounts share the single most common handle stem.
        stems: Counter = Counter()
        for h, n in acc_handles.items():
            st = _stem(h)
            if st:
                stems[st] += n
        dominant_stem, dominant_n = (stems.most_common(1)[0] if stems else ("", 0))
        consistency = round(dominant_n / total_accounts, 2) if total_accounts else 0.0

        numeric_suffix = sum(1 for h in distinct if re.search(r"\d$", h))
        leet = sum(1 for h in distinct if _looks_leet(h))
        separators = Counter()
        for h in distinct:
            for sep in (".", "_", "-"):
                if sep in h:
                    separators[sep] += 1

        # avatar reuse: how widely one profile photo recurs
        avatar_reuse = f["phashes"].most_common(1)[0][1] if f["phashes"] else 0
        distinct_photos = len(f["phashes"])

        # bio consistency: average pairwise similarity
        bio_texts = [t for _, t in f["bios"]]
        bio_consistency = self._avg_similarity(bio_texts)

        # composite cross-platform consistency 0–100
        score = 0.0
        score += 55 * consistency
        score += 20 * (1.0 if avatar_reuse >= 2 else 0.0)
        score += 15 * bio_consistency
        score += 10 * (1.0 if (n_distinct and numeric_suffix / n_distinct < 0.5) else 0.0)
        cross = round(min(100.0, score), 1)

        style = []
        if n_distinct:
            if numeric_suffix / n_distinct >= 0.4:
                style.append("appends numeric suffixes")
            if leet:
                style.append("uses leet-speak substitutions")
            if separators:
                sep = separators.most_common(1)[0][0]
                style.append(f"prefers '{sep}' as a separator")
            if consistency >= 0.6:
                style.append("highly consistent handle across platforms")
            elif consistency >= 0.3:
                style.append("partially consistent handle")
            else:
                style.append("varied handles across platforms")

        return {
            "dominant_handle": dominant_stem,
            "handle_consistency": consistency,
            "distinct_handles": n_distinct,
            "uses_numeric_suffix": bool(n_distinct and numeric_suffix / n_distinct >= 0.4),
            "uses_leet": bool(leet),
            "preferred_separator": (separators.most_common(1)[0][0] if separators else None),
            "avatar_reuse_count": avatar_reuse,
            "distinct_avatars": distinct_photos,
            "bio_consistency": bio_consistency,
            "cross_platform_consistency": cross,
            "style_notes": style,
        }

    @staticmethod
    def _avg_similarity(texts: list[str]) -> float:
        import difflib
        if len(texts) < 2:
            return 0.0
        ratios, n = 0.0, 0
        for i in range(len(texts)):
            for j in range(i + 1, len(texts)):
                ratios += difflib.SequenceMatcher(None, texts[i], texts[j]).ratio()
                n += 1
        return round(ratios / n, 2) if n else 0.0

    # ----------------------------------------------------------------- temporal
    def _temporal(self, f: dict) -> dict:
        # Only *account-creation* dates from enrichment count as subject activity.
        # Collection timestamps (``first_seen``) describe when WE looked, not when
        # the subject was active, so they must never drive the activity window.
        created = sorted(f["created"], key=lambda x: x[1])

        timeline = [
            {"platform": p, "date": dt.isoformat(), "kind": "account_created"}
            for p, dt in created
        ]
        earliest = created[0][1] if created else None
        latest = created[-1][1] if created else None
        span_days = (latest - earliest).days if (earliest and latest) else 0

        active_era = None
        if created:
            years = sorted({dt.year for _, dt in created})
            active_era = f"{years[0]}–{years[-1]}" if years[0] != years[-1] else str(years[0])

        return {
            "earliest_activity": earliest.isoformat() if earliest else None,
            "latest_activity": latest.isoformat() if latest else None,
            "active_span_days": span_days,
            "active_era": active_era,
            "account_creations": len(created),
            "creation_timeline": timeline,
            "activity_pattern": self._activity_pattern(f.get("activity_times", [])),
        }

    @staticmethod
    def _activity_pattern(times: list) -> dict | None:
        """Infer a likely UTC offset / region from when the subject is active.

        Buckets subject-activity timestamps (account creations + EXIF capture
        times, all UTC) into an hour-of-day histogram, finds the longest quiet
        window (the probable sleep period), and maps its centre to a local ~03:00
        to estimate the UTC offset. This geolocates a subject who hides their
        location. Honest about uncertainty: requires several samples and is
        always flagged as a soft, corroborate-first lead.
        """
        hrs = [t.hour for t in times if hasattr(t, "hour")]
        n = len(hrs)
        if n < 5:
            return None
        hist = [0] * 24
        for h in hrs:
            hist[h] += 1
        # longest contiguous (wrap-around) 6h window with the least activity.
        best_start, best_sum = 0, None
        for start in range(24):
            window = sum(hist[(start + i) % 24] for i in range(6))
            if best_sum is None or window < best_sum:
                best_sum, best_start = window, start
        quiet_centre = (best_start + 3) % 24          # middle of the 6h quiet block
        offset = int(round(3 - quiet_centre))          # local ~03:00 at quiet centre
        offset = ((offset + 12) % 24) - 12             # normalise to (-12, +12]
        busiest = max(range(24), key=lambda h: hist[h])

        regions = {
            5: "India / South Asia (UTC+5:30)", 6: "South Asia / Bangladesh (UTC+6)",
            0: "UK / West Africa (UTC+0)", 1: "Central Europe (UTC+1)",
            2: "Eastern Europe (UTC+2)", 3: "East Africa / Middle East (UTC+3)",
            4: "Gulf / West Asia (UTC+4)", 7: "SE Asia (UTC+7)", 8: "China / SE Asia (UTC+8)",
            9: "Japan / Korea (UTC+9)", -5: "US Eastern (UTC-5)", -6: "US Central (UTC-6)",
            -7: "US Mountain (UTC-7)", -8: "US Pacific (UTC-8)", -3: "Brazil / Argentina (UTC-3)",
        }
        region = regions.get(offset, f"UTC{offset:+d}")
        sign = "+" if offset >= 0 else "-"
        return {
            "sample_size": n,
            "hours_utc": hist,
            "busiest_hour_utc": busiest,
            "quiet_window_utc": f"{best_start:02d}:00–{(best_start + 6) % 24:02d}:00",
            "inferred_utc_offset": f"{sign}{abs(offset)}",
            "inferred_region": region,
            "confidence": "moderate" if n >= 12 else "low",
            "note": ("Inferred from when the subject created accounts / captured "
                     "photos (UTC); a soft geolocation lead, corroborate before use."),
        }

    # ---------------------------------------------------------------- interests
    def _interests(self, f: dict) -> dict:
        cat_counts: Counter = Counter()
        cat_weight: Counter = Counter()
        cat_platforms: dict[str, list[str]] = defaultdict(list)
        for acc in f["accounts"]:
            cat = PLATFORM_CATEGORY.get(acc["platform"], "other")
            cat_counts[cat] += 1
            # Weight the *interest* signal by corroboration: a platform confirmed
            # by several tools (or via a first-party API, tier <= 2) reflects a
            # genuine account far more than a lone username-availability hit.
            weight = len(acc["tools"]) or 1
            if acc.get("tier", 9) <= 2:
                weight += 2
            cat_weight[cat] += weight
            cat_platforms[cat].append(acc["platform"])

        categories = [
            {
                "category": cat,
                "label": _CATEGORY_LABEL.get(cat, cat.title()),
                "platform_count": n,
                "platforms": sorted(set(cat_platforms[cat]))[:12],
            }
            for cat, n in cat_counts.most_common()
        ]

        dev = cat_counts.get("developer", 0)
        if dev >= 3:
            soph = ("High", "Strong developer/technical footprint across multiple code platforms.")
        elif dev >= 1:
            soph = ("Moderate", "Some technical-platform presence.")
        else:
            soph = ("Low / unknown", "No clear technical-platform presence detected.")

        # "Other" is a catch-all for un-categorised niche sites — it is never a
        # meaningful *interest*. Rank the categorised areas by corroborated
        # presence; only crown a single "primary" when one area clearly leads,
        # otherwise report the footprint as spanning several areas (honest for
        # username-enumeration results, which spread thinly across categories).
        meaningful = sorted(
            (c for c in categories if c["category"] != "other"),
            key=lambda c: cat_weight[c["category"]], reverse=True,
        )
        top_interests = [c["label"] for c in meaningful[:3]]
        primary = None
        if meaningful:
            top_w = cat_weight[meaningful[0]["category"]]
            second_w = cat_weight[meaningful[1]["category"]] if len(meaningful) > 1 else 0
            if second_w == 0 or top_w >= 1.3 * second_w:
                primary = meaningful[0]["label"]

        return {
            "categories": categories,
            "primary_interest": primary,
            "top_interests": top_interests,
            "tech_sophistication": {"level": soph[0], "rationale": soph[1]},
        }

    # ---------------------------------------------------------------- footprint
    def _footprint(self, f: dict, attributes: dict) -> dict:
        platforms = {acc["platform"] for acc in f["accounts"]}
        corroborated = [acc for acc in f["accounts"] if len(acc["tools"]) >= 2]
        breadth = len(platforms)
        depth = len(corroborated)

        # 0–100 footprint score: breadth (log) + depth + identifier richness
        score = 0.0
        score += min(45.0, 13.0 * math.log2(breadth + 1))
        score += min(25.0, 5.0 * depth)
        score += 10.0 if attributes["names"] else 0.0
        score += 8.0 if attributes["locations"] else 0.0
        score += min(12.0, 3.0 * len(f["emails"]))
        footprint_score = round(min(100.0, score), 1)

        if footprint_score >= 70:
            visibility = "Extensive"
        elif footprint_score >= 40:
            visibility = "Moderate"
        elif footprint_score >= 15:
            visibility = "Limited"
        else:
            visibility = "Minimal"

        return {
            "platform_count": breadth,
            "confirmed_accounts": depth,
            "total_accounts": len(f["accounts"]),
            "footprint_score": footprint_score,
            "visibility": visibility,
        }

    # ----------------------------------------------------- behavioral indicators
    def _behavioral_indicators(
        self, f: dict, behavior: dict, interests: dict, temporal: dict,
        footprint: dict, attributes: dict,
    ) -> dict:
        """Inferred behavioural indicators + an OPSEC posture read.

        Pure pattern recognition over already-collected features (no new
        collection). Every item is an *investigative lead*, never a
        determination — surfaced with its rationale and the evidence behind it.
        """
        platforms = {acc["platform"] for acc in f["accounts"]}
        indicators: list[dict] = []

        def add(indicator: str, level: str, rationale: str, evidence: list) -> None:
            indicators.append({
                "indicator": indicator, "level": level, "rationale": rationale,
                "evidence": sorted({e for e in evidence if e})[:8],
            })

        has_real_name = bool(attributes.get("names"))
        hc = behavior.get("handle_consistency", 0.0) or 0.0
        distinct = behavior.get("distinct_handles", 0) or 0

        sec = sorted(platforms & _SECURITY_PLATFORMS)
        if sec:
            add("offensive_security_affinity", "high" if len(sec) >= 2 else "notable",
                "Presence on security/CTF/exploit platforms indicates hands-on technical capability.",
                sec)

        priv = sorted(platforms & _PRIVACY_PLATFORMS)
        if priv and not has_real_name:
            add("anonymity_conscious", "notable",
                "Uses privacy-oriented platforms with no exposed real name — possible OPSEC awareness.",
                priv)

        if hc >= 0.6 and distinct >= 2:
            add("high_handle_reuse", "high",
                f"Reuses one handle across ~{int(hc * 100)}% of accounts — highly trackable; strong pivot value.",
                [behavior.get("dominant_handle")])
        elif 0 < hc < 0.3 and distinct >= 3:
            add("identity_fragmentation", "notable",
                "Varied handles across platforms — possible deliberate compartmentalisation or multiple personas.",
                [])

        if behavior.get("avatar_reuse_count", 0) >= 2:
            add("avatar_reuse", "notable",
                "The same profile photo recurs across accounts — strong visual cross-linking signal.",
                [])

        span = temporal.get("active_span_days", 0) or 0
        creations = temporal.get("account_creations", 0) or 0
        if creations >= 3 and 0 < span <= 30:
            add("burst_account_creation", "notable",
                f"{creations} accounts created within {span} day(s) — burst pattern (single-purpose / coordinated).",
                [])
        elif span >= 1095:
            add("long_tenured_presence", "info",
                f"Accounts span ~{span // 365} year(s) — long-tenured, organically grown footprint.",
                [])

        opsec = 0
        opsec += 2 if (priv and not has_real_name) else 0
        opsec += 1 if (0 < hc < 0.3) else 0
        opsec -= 1 if has_real_name else 0
        opsec -= 1 if hc >= 0.6 else 0
        opsec -= 1 if behavior.get("avatar_reuse_count", 0) >= 2 else 0
        if opsec >= 2:
            posture = ("high", "Privacy-aware: compartmentalised identifiers and anonymity-oriented platforms.")
        elif opsec <= -1:
            posture = ("low", "Low operational security: reused handles/photos or an exposed real identity ease tracking.")
        else:
            posture = ("moderate", "Mixed operational-security signals.")

        return {
            "indicators": indicators,
            "operational_security": {"level": posture[0], "rationale": posture[1]},
            "note": "Inferred behavioural indicators — investigative leads only, not determinations.",
        }

    # --------------------------------------------------------------- reasoning
    def _reasoning(self, identity, attributes, behavior, temporal, interests, footprint) -> list[dict]:
        chains: list[dict] = []

        def add(claim, confidence, evidence):
            chains.append({
                "claim": claim,
                "confidence": round(float(confidence), 2),
                "evidence": [e for e in evidence if e],
            })

        if attributes["names"]:
            top = attributes["names"][0]
            add(
                f"Likely real name: {top['value']}",
                top["confidence"],
                [f"{s} reported name '{top['value']}'" for s in top["sources"][:4]]
                or [f"name observed {top['observations']}×"],
            )
        if attributes["locations"] or attributes["phone_region"]:
            bits, conf = [], 0.4
            if attributes["locations"]:
                loc = attributes["locations"][0]
                bits.append(f"profile location '{loc['value']}'")
                conf = max(conf, loc["confidence"])
            if attributes["phone_region"]:
                bits.append(f"phone country → {attributes['phone_region']}")
                conf = max(conf, 0.55)
            if attributes["timezone"]:
                bits.append(f"timezone {attributes['timezone']}")
            add(f"Geographic association: {bits[0]}", conf, bits)
        if behavior["dominant_handle"] and behavior["handle_consistency"] >= 0.3:
            add(
                f"Operates a consistent persona under the handle stem "
                f"'{behavior['dominant_handle']}'",
                min(0.95, 0.4 + behavior["handle_consistency"]),
                [f"handle reused on {int(behavior['handle_consistency'] * behavior['distinct_handles'])}"
                 f"/{behavior['distinct_handles']} observed handles",
                 f"cross-platform consistency {behavior['cross_platform_consistency']}/100"],
            )
        if behavior["avatar_reuse_count"] >= 2:
            add(
                f"Reuses the same profile photo across {behavior['avatar_reuse_count']} platforms",
                0.8,
                [f"matching image hash on {behavior['avatar_reuse_count']} accounts"],
            )
        if interests["tech_sophistication"]["level"] != "Low / unknown":
            add(
                f"Technical sophistication: {interests['tech_sophistication']['level']}",
                0.7,
                [interests["tech_sophistication"]["rationale"]],
            )
        if temporal["active_era"]:
            add(
                f"Online activity spans {temporal['active_era']}",
                0.6,
                [f"{temporal['account_creations']} dated account creation(s)",
                 f"active span ≈ {temporal['active_span_days']} days"],
            )
        if attributes["occupation"]:
            add(
                f"Probable occupation/field: {', '.join(attributes['occupation'])}",
                0.55,
                ["keyword match in bio/headline text"],
            )
        contacts = attributes.get("contact_numbers") or []
        full = [c for c in contacts if not c.get("obfuscated")]
        masked = [c for c in contacts if c.get("obfuscated")]
        if full:
            c = full[0]
            add(
                f"Recovered contact number {c['value']}",
                c["confidence"],
                [f"surfaced via {s}" for s in c["sources"][:3]]
                or ["exposed by a linked account"],
            )
        elif masked:
            c = masked[0]
            add(
                f"Partial contact-number lead {c['value']} (masked)",
                c["confidence"],
                [f"surfaced via {s}" for s in c["sources"][:3]]
                or ["masked number exposed by a linked account"],
            )
        chains.sort(key=lambda c: -c["confidence"])
        return chains

    # -------------------------------------------------------------- completeness
    @staticmethod
    def _completeness(identity, attributes, behavior) -> dict:
        checks = {
            "name": bool(attributes["names"]),
            "location": bool(attributes["locations"] or attributes["phone_region"]),
            "email": bool(identity["emails"]),
            "phone": bool(identity["phones"]),
            "photo": behavior["avatar_reuse_count"] > 0 or behavior["distinct_avatars"] > 0,
            "occupation": bool(attributes["occupation"] or attributes["affiliations"]),
            "handle": bool(behavior["dominant_handle"]),
        }
        have = sum(1 for v in checks.values() if v)
        return {
            "score": round(100 * have / len(checks), 1),
            "known": [k for k, v in checks.items() if v],
            "missing": [k for k, v in checks.items() if not v],
        }

    # ----------------------------------------------------------------- summary
    def _summary(self, identity, attributes, behavior, temporal, interests, footprint, case) -> str:
        subject = case.get("seed_value") or "the subject"
        parts: list[str] = []

        who = f"Subject '{subject}'"
        connector = "maintains"
        if attributes["names"]:
            who += f" is likely **{attributes['names'][0]['value']}**"
            connector = "who maintains"
        visibility = footprint["visibility"].lower()
        article = "an" if visibility[:1] in "aeiou" else "a"
        parts.append(
            f"{who} {connector} {article} {visibility} digital footprint "
            f"({footprint['footprint_score']}/100) spanning {footprint['platform_count']} "
            f"platform(s), with {footprint['confirmed_accounts']} corroborated account(s)."
        )

        geo = []
        if attributes["locations"]:
            geo.append(attributes["locations"][0]["value"])
        pr = attributes["phone_region"]
        if pr and not any(pr.lower() in g.lower() for g in geo):
            geo.append(pr)
        if geo:
            parts.append(f"Geographic signals point to {', '.join(dict.fromkeys(geo))}.")

        full_phones = [c for c in attributes.get("contact_numbers", []) if not c.get("obfuscated")]
        if full_phones:
            parts.append(
                f"A contact number ({full_phones[0]['value']}) was recovered "
                f"from a linked account."
            )

        if behavior["dominant_handle"]:
            parts.append(
                f"They operate primarily under the handle stem '{behavior['dominant_handle']}' "
                f"(cross-platform consistency {behavior['cross_platform_consistency']}/100"
                + (f"; {behavior['style_notes'][0]}" if behavior["style_notes"] else "")
                + ")."
            )

        if interests["primary_interest"]:
            soph = interests["tech_sophistication"]["level"].lower()
            parts.append(
                f"Their primary online activity is **{interests['primary_interest']}**, "
                f"with {soph} technical sophistication."
            )
        elif interests.get("top_interests"):
            soph = interests["tech_sophistication"]["level"].lower()
            tops = interests["top_interests"]
            joined = tops[0] if len(tops) == 1 else ", ".join(tops[:-1]) + f" and {tops[-1]}"
            parts.append(
                f"Their footprint spans several areas — {joined} — "
                f"with {soph} technical sophistication."
            )

        if temporal["active_era"]:
            parts.append(f"Observed online activity spans {temporal['active_era']}.")

        if attributes["occupation"]:
            parts.append(f"Bio/headline text suggests a background in {', '.join(attributes['occupation'])}.")

        return " ".join(parts)

    def _ai_summary(
        self, identity, attributes, behavior, temporal, interests, footprint, case, deterministic
    ) -> str | None:
        """Optional local-LLM dossier summary, grounded in the derived attributes.
        Returns None when Ollama is disabled/unreachable so the caller keeps the
        deterministic summary."""
        try:
            from api.services.llm_narrative import LLMNarrator
        except Exception:  # noqa: BLE001
            return None
        facts = {
            "seed": case.get("seed_value"),
            "candidate_names": [n.get("value") for n in attributes.get("names", [])[:3]],
            "locations": [loc.get("value") for loc in attributes.get("locations", [])[:3]],
            "occupation": attributes.get("occupation"),
            "phone_region": attributes.get("phone_region"),
            "dominant_handle": behavior.get("dominant_handle"),
            "cross_platform_consistency": behavior.get("cross_platform_consistency"),
            "primary_interest": interests.get("primary_interest"),
            "top_interests": interests.get("top_interests"),
            "tech_sophistication": (interests.get("tech_sophistication") or {}).get("level"),
            "active_era": temporal.get("active_era"),
            "footprint": {
                "visibility": footprint.get("visibility"),
                "score": footprint.get("footprint_score"),
                "platform_count": footprint.get("platform_count"),
                "confirmed_accounts": footprint.get("confirmed_accounts"),
            },
            "deterministic_summary": deterministic,
        }
        instruction = (
            "Write a subject dossier summary for an investigator: who the footprint "
            "suggests the subject may be, where they appear active, their primary "
            "interests and technical sophistication, and their online tenure. Treat "
            "all attributes as unverified investigative leads requiring confirmation."
        )
        return LLMNarrator().generate(facts, instruction)

    # --------------------------------------------------------- SDM extraction
    @staticmethod
    def _sdm_data(live: list[dict]) -> tuple[dict, dict, list]:
        """Extract SDM behavioral/network/community data from evidence enrichment.

        Returns (sdm_behavioral, sdm_network, sdm_communities).
        """
        sdm_behavioral: dict = {}
        sdm_network: dict = {}
        sdm_communities: list = []

        for e in live:
            notes = e.get("notes") or ""
            enrich = e.get("platform_enrichment")
            if not isinstance(enrich, dict):
                continue
            rt = e.get("result_type", "")

            # Behavioral insights
            if rt == "behavioral_insight" or "[behavioral-inferred]" in notes:
                if enrich.get("inferred_timezone") and "inferred_timezone" not in sdm_behavioral:
                    sdm_behavioral["inferred_timezone"] = enrich["inferred_timezone"]
                if enrich.get("rhythm_breaks"):
                    sdm_behavioral.setdefault("rhythm_breaks", []).extend(enrich["rhythm_breaks"])
                if enrich.get("velocity_spikes"):
                    sdm_behavioral.setdefault("velocity_spikes", []).extend(enrich["velocity_spikes"])

            # Post timeline → posting frequency
            if rt == "post_timeline_collected":
                if enrich.get("posting_frequency") and "posting_frequency" not in sdm_behavioral:
                    sdm_behavioral["posting_frequency"] = enrich["posting_frequency"]

            # Interaction graph
            if enrich.get("interaction_graph") and isinstance(enrich["interaction_graph"], dict):
                for target, count in enrich["interaction_graph"].items():
                    sdm_network[target] = sdm_network.get(target, 0) + count

            # Community memberships
            if rt == "community_membership_found":
                comm = enrich.get("community")
                if isinstance(comm, dict) and comm not in sdm_communities:
                    sdm_communities.append(comm)

        return sdm_behavioral, sdm_network, sdm_communities

