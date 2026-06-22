"""Tests for the 8 new OSINT tool adapters.

Each test validates the parse() method with fixture data. Network calls are
monkeypatched. Adapters are imported individually (never fallback_chain) so
they stay importable on the Windows host.
"""
from __future__ import annotations
from uuid import uuid4

# --- Helpers ----------------------------------------------------------------
class _FakeResp:
    def __init__(self, payload=None, text="", status_code=200):
        self._payload = payload
        self.text = text
        self.status_code = status_code
        self.url = "https://example.com"
    def json(self):
        return self._payload

def _ctx(adapter, seed="test@example.com", seed_type="email"):
    adapter._case_id = uuid4()
    adapter._run_id = uuid4()
    adapter._analyst_id = "analyst-1"
    adapter._seed_type = seed_type
    adapter._seed_value = seed


# ---------------------------------------------------------------------------
# EmailRep
# ---------------------------------------------------------------------------
def test_emailrep_parses_reputation():
    from worker_python.adapters.email.emailrep import EmailRepAdapter
    adapter = EmailRepAdapter()
    _ctx(adapter, "victim@example.com", "email")
    raw = [{
        "email": "victim@example.com",
        "reputation": "low",
        "suspicious": True,
        "references": 5,
        "details": {
            "malicious_activity": True,
            "credentials_leaked": True,
            "spam": False,
            "blacklisted": False,
        },
    }]
    units = adapter.parse(raw)
    assert len(units) == 1
    unit = units[0]
    assert unit.result_type == "email_reputation"
    assert unit.source_platform == "emailrep"
    assert "reputation=low" in (unit.notes or "")
    assert "malicious_activity" in (unit.notes or "")
    # Must NOT be account_found
    assert unit.result_type != "account_found"

def test_emailrep_skips_non_email():
    from worker_python.adapters.email.emailrep import EmailRepAdapter
    adapter = EmailRepAdapter()
    _ctx(adapter, "just_a_handle", "username")
    assert adapter.run("just_a_handle") == []


# ---------------------------------------------------------------------------
# Epieos
# ---------------------------------------------------------------------------
def test_epieos_parses_linked_accounts():
    from worker_python.adapters.email.epieos import EpieosAdapter
    adapter = EpieosAdapter()
    _ctx(adapter, "victim@example.com", "email")
    raw = [
        {"service": "Google", "avatar_url": "https://cdn.example.com/avatar.jpg"},
        {"service": "Skype", "avatar_url": None},
    ]
    units = adapter.parse(raw)
    assert len(units) == 2
    assert all(u.result_type == "account_found" for u in units)
    # First unit should have avatar_url in enrichment for photo-hash pipeline
    assert units[0].platform_enrichment.get("avatar_url") == "https://cdn.example.com/avatar.jpg"
    assert units[0].source_platform == "google"

def test_epieos_empty_service_ignored():
    from worker_python.adapters.email.epieos import EpieosAdapter
    adapter = EpieosAdapter()
    _ctx(adapter, "victim@example.com", "email")
    raw = [{"service": "", "avatar_url": None}]
    units = adapter.parse(raw)
    assert len(units) == 0


# ---------------------------------------------------------------------------
# Ahmia
# ---------------------------------------------------------------------------
def test_ahmia_parses_onion_results():
    from worker_python.adapters.passive.ahmia import AhmiaAdapter
    adapter = AhmiaAdapter()
    _ctx(adapter, "target_handle", "username")
    raw = [
        {"url": "http://abc123.onion/page", "title": "Dark Market Listing", "snippet": "mentions target_handle"},
        {"url": "http://def456.onion/forum", "title": "Forum Post", "snippet": ""},
    ]
    units = adapter.parse(raw)
    assert len(units) == 2
    assert all(u.result_type == "onion_hit" for u in units)
    assert all(u.source_platform == "darkweb" for u in units)
    assert "ahmia" in (units[0].notes or "")


# ---------------------------------------------------------------------------
# Censys
# ---------------------------------------------------------------------------
def test_censys_parses_host_data():
    from worker_python.adapters.platform.censys import CensysAdapter
    adapter = CensysAdapter()
    _ctx(adapter, "example.com", "domain")
    raw = [
        {"kind": "summary", "target": "example.com", "host_count": 2, "total": 2},
        {"ip": "93.184.216.34", "services": [{"port": 443, "service_name": "HTTPS", "transport_protocol": "TCP"}], "location": {}, "autonomous_system": {}},
    ]
    units = adapter.parse(raw)
    assert len(units) == 2
    assert units[0].result_type == "domain_hit"
    assert units[0].result_value == "example.com"
    assert units[1].result_value == "93.184.216.34"
    assert "censys" in (units[0].notes or "")


# ---------------------------------------------------------------------------
# DNS Dumpster
# ---------------------------------------------------------------------------
def test_dnsdumpster_parses_subdomains():
    from worker_python.adapters.platform.dnsdumpster import DnsDumpsterAdapter
    adapter = DnsDumpsterAdapter()
    _ctx(adapter, "example.com", "domain")
    raw = [
        {"kind": "summary", "target": "example.com", "record_count": 2},
        {"kind": "subdomain", "value": "mail.example.com"},
        {"kind": "subdomain", "value": "api.example.com"},
    ]
    units = adapter.parse(raw)
    assert len(units) == 3
    assert units[0].result_type == "domain_hit"
    assert units[0].result_value == "example.com"
    assert units[1].result_value == "mail.example.com"
    assert "dnsdumpster" in (units[1].notes or "")


# ---------------------------------------------------------------------------
# Reddit Intel
# ---------------------------------------------------------------------------
def test_reddit_intel_parses_user_profile():
    from worker_python.adapters.platform.reddit_intel import RedditIntelAdapter
    adapter = RedditIntelAdapter()
    _ctx(adapter, "target_user", "username")
    raw = [{
        "name": "target_user",
        "id": "abc123",
        "link_karma": 1500,
        "comment_karma": 3200,
        "created_utc": 1600000000.0,
        "is_gold": False,
        "verified": True,
        "has_verified_email": True,
        "icon_img": "https://styles.redditmedia.com/avatar.png?format=webp",
        "snoovatar_img": "",
        "subreddit": {"public_description": "I am a test user"},
    }]
    units = adapter.parse(raw)
    assert len(units) == 1
    unit = units[0]
    assert unit.result_type == "account_found"
    assert "reddit.com" in unit.source_platform
    assert "reddit.com/user/target_user" in unit.result_value
    assert unit.platform_enrichment["avatar_url"] == "https://styles.redditmedia.com/avatar.png"
    assert unit.platform_enrichment["bio"] == "I am a test user"
    assert "karma=" in (unit.notes or "")


# ---------------------------------------------------------------------------
# Archive.today (best-effort, never raises)
# ---------------------------------------------------------------------------
def test_archive_today_best_effort():
    from api.services.preservation import PreservationService
    svc = PreservationService()
    # Should return None on failure, never raise
    result = svc._archive_today_check("https://nonexistent-test-url.example.com")
    assert result is None


# ---------------------------------------------------------------------------
# Picarta AI geolocation
# ---------------------------------------------------------------------------
def test_picarta_ai_geolocation_disabled_returns_none():
    from api.services.photo_hash import PhotoHasher
    hasher = PhotoHasher()
    # With no API key set, should return None
    result = hasher._ai_geolocate(b"\x89PNG\r\n\x1a\n\x00\x00\x00")
    assert result is None
