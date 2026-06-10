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

# URL path/query tokens that are API endpoints or pages, never real handles.
_API_NOISE = {
    "api", "public", "users", "user", "search", "details", "profile", "profiles",
    "autocomplete", "advancedsearch", "getprofile", "publications", "actor",
    "people", "member", "members", "lookup", "query", "find", "account",
    "accounts", "v1", "v2", "v3", "v4", "graphql", "rest", "json", "oauth",
    "auth", "about", "home", "index", "explore", "results", "page", "settings",
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


class ProfileEngine:
    """Synthesise raw evidence into an explainable subject dossier."""

    # ------------------------------------------------------------------- API
    def build(
        self,
        evidence: list[dict],
        links: list[dict] | None = None,
        case: dict | None = None,
        persona: dict | None = None,
    ) -> dict:
        """Return the full subject dossier for one case (pure function)."""
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
        reasoning = self._reasoning(identity, attributes, behavior, temporal, interests, footprint)
        completeness = self._completeness(identity, attributes, behavior)
        summary = self._summary(identity, attributes, behavior, temporal, interests, footprint, case)

        return {
            "generated_at": _now().isoformat(),
            "case_id": str(case.get("case_id", "")),
            "identity": identity,
            "attributes": attributes,
            "behavioral_fingerprint": behavior,
            "temporal": temporal,
            "interests": interests,
            "footprint": footprint,
            "reasoning": reasoning,
            "profile_completeness": completeness,
            "persona_count": (persona or {}).get("persona_count"),
            "summary": summary,
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
            "phones": Counter(),
            "usernames": Counter(),
            "username_sources": defaultdict(set),
            "seed_usernames": set(),
            "account_handles": Counter(),   # clean per-account handle -> #accounts
            "verified_platforms": set(),
            "created": [],       # list of (platform, datetime)
            "first_seen": [],    # list of (platform, datetime)
            "accounts": [],      # confirmed/profile accounts: {platform, handle, tier, tools}
            "platform_tiers": defaultdict(lambda: 99),
            "platform_tools": defaultdict(set),
        }

        # seed identifiers
        for seed in (case.get("seed_value"), *[e.get("seed_value") for e in live]):
            self._bucket_seed(str(seed or ""), f)

        accounts: dict[tuple, dict] = {}
        for e in live:
            tool = e.get("tool_name") or "?"
            platform = _platform_of(e)
            rt = e.get("result_type")
            tier = e.get("source_tier")
            tier = int(tier) if tier is not None else 2
            val = e.get("result_value") or ""

            # emails from any result value (e.g. theHarvester)
            for m in _EMAIL_RE.findall(val):
                f["emails"][m.lower()] += 1

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
            if handle:
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
            f["emails"][raw.lower()] += 1
        elif raw.startswith("+") or (raw.replace(" ", "").replace("-", "").isdigit() and len(raw) >= 7):
            f["phones"][re.sub(r"[\s\-]", "", raw)] += 1
        elif not raw.startswith("http"):
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
                f["emails"][m.group(0).lower()] += 1
        for k in EMAIL_LIST_KEYS:
            for v in enrich.get(k) or []:
                if isinstance(v, str) and (m := _EMAIL_RE.search(v)):
                    f["emails"][m.group(0).lower()] += 1
        for k in PHONE_KEYS:
            v = enrich.get(k)
            if v:
                digits = re.sub(r"[^0-9]", "", str(v))
                if len(digits) >= 7:
                    f["phones"][f"+{digits}"] += 1
        for k in USERNAME_KEYS:
            v = enrich.get(k)
            if isinstance(v, str) and v.strip() and "@" not in v:
                f["usernames"][v.strip().lower()] += 1
                f["username_sources"][v.strip().lower()].add(tool)
        for k in USERNAME_LIST_KEYS:
            for v in enrich.get(k) or []:
                if isinstance(v, str) and v.strip():
                    f["usernames"][v.strip().lower()] += 1
                    f["username_sources"][v.strip().lower()].add(tool)
        for k in VERIFIED_KEYS:
            if enrich.get(k) is True:
                f["verified_platforms"].add(platform)
        if (created := _first(enrich, CREATED_KEYS)) and (dt := _parse_dt(created)):
            f["created"].append((platform, dt))

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
        if re.fullmatch(r"[A-Za-z0-9_\-]{2,30}", seg) and seg.lower() not in _API_NOISE:
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

        # languages
        attrs["languages"] = [
            {"value": lang, "observations": freq}
            for lang, freq in f["languages"].most_common(5) if lang
        ]

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
        return attrs

    @staticmethod
    def _conf(freq: int, n_sources: int) -> float:
        """Confidence 0–1 from how often and how broadly a value was observed."""
        base = 0.35 + 0.18 * math.log2(freq + 1) + 0.12 * max(0, n_sources - 1)
        return round(min(0.97, base), 2)

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
        if attributes["phone_region"]:
            geo.append(attributes["phone_region"])
        if geo:
            parts.append(f"Geographic signals point to {', '.join(dict.fromkeys(geo))}.")

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
