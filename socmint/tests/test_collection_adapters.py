"""Tests for the new identifier-based collection adapters (breach + forum sweep).

These cover the keyless breach-analysis adapters (XposedOrNot, Hudson Rock,
ProxyNova) and the forum/blog/comment sweep. Network calls are monkeypatched, so
the tests validate parsing, evidence shape, and — critically — that ProxyNova
never emits plaintext credentials. They import the adapters individually (never
``fallback_chain``) so they stay importable on the Windows host.
"""
from __future__ import annotations

from uuid import uuid4

import worker_python.adapters.email.xposedornot as xon_mod
import worker_python.adapters.email.hudsonrock as hr_mod
import worker_python.adapters.email.proxynova as pn_mod
import worker_python.adapters.passive.forum_sweep as fs_mod
from worker_python.adapters.email.xposedornot import XposedOrNotAdapter
from worker_python.adapters.email.hudsonrock import HudsonRockAdapter
from worker_python.adapters.email.proxynova import ProxyNovaAdapter, _mask_identifier
from worker_python.adapters.passive.forum_sweep import ForumSweepAdapter


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _ctx(adapter, seed, seed_type):
    adapter._case_id = uuid4()
    adapter._run_id = uuid4()
    adapter._analyst_id = "analyst-1"
    adapter._seed_type = seed_type
    adapter._seed_value = seed


# --------------------------------------------------------------------------- #
# XposedOrNot
# --------------------------------------------------------------------------- #
def test_xposedornot_parses_named_breaches_and_flags_username_exposure(monkeypatch):
    payload = {
        "ExposedBreaches": {
            "breaches_details": [
                {
                    "breach": "SweClockers",
                    "domain": "sweclockers.com",
                    "xposed_data": "Usernames;Email addresses;Passwords",
                    "xposed_date": "2015",
                    "xposed_records": 254967,
                    "password_risk": "hardtocrack",
                }
            ]
        }
    }
    monkeypatch.setattr(xon_mod, "http_get", lambda *a, **k: _FakeResp(payload))
    adapter = XposedOrNotAdapter()
    _ctx(adapter, "victim@example.com", "email")

    units = adapter.parse(adapter.run("victim@example.com"))
    assert len(units) == 1
    unit = units[0]
    assert unit.result_type == "breach_hit"
    assert unit.source_platform == "breach"
    assert unit.result_value == "SweClockers"
    assert "exposed=Usernames" in (unit.notes or "")
    assert "association=email" in (unit.notes or "")  # username exposure flagged


def test_xposedornot_ignores_non_email_seed(monkeypatch):
    monkeypatch.setattr(xon_mod, "http_get", lambda *a, **k: _FakeResp({}))
    adapter = XposedOrNotAdapter()
    _ctx(adapter, "just_a_handle", "username")
    assert adapter.run("just_a_handle") == []


# --------------------------------------------------------------------------- #
# Hudson Rock
# --------------------------------------------------------------------------- #
def test_hudsonrock_parses_infostealer_with_linked_logins(monkeypatch):
    payload = {
        "stealers": [
            {
                "date_compromised": "2026-06-12T10:45:59.000Z",
                "operating_system": "Windows 11 Home",
                "total_user_services": 69,
                "total_corporate_services": 0,
                "top_logins": ["s*****@gmail.com", "a*****@student.edu"],
            }
        ]
    }
    monkeypatch.setattr(hr_mod, "http_get", lambda *a, **k: _FakeResp(payload))
    adapter = HudsonRockAdapter()
    _ctx(adapter, "victim@example.com", "email")

    units = adapter.parse(adapter.run("victim@example.com"))
    assert len(units) == 1
    unit = units[0]
    assert unit.result_type == "breach_hit"
    assert unit.result_value.startswith("infostealer:")
    assert "user_services=69" in (unit.notes or "")
    assert "linked_logins=" in (unit.notes or "")


def test_hudsonrock_accepts_username_seed(monkeypatch):
    monkeypatch.setattr(hr_mod, "http_get", lambda *a, **k: _FakeResp({"stealers": []}))
    adapter = HudsonRockAdapter()
    _ctx(adapter, "coolhandle", "username")
    # No stealers → empty result (chain records an 'unavailable' marker upstream).
    assert adapter.run("coolhandle") == []


# --------------------------------------------------------------------------- #
# ProxyNova — must never leak plaintext credentials
# --------------------------------------------------------------------------- #
def test_proxynova_masks_identifiers():
    assert _mask_identifier("john.doe@gmail.com") == "j*******@gmail.com"
    assert _mask_identifier("john.doe") == "j*******"


def test_proxynova_never_emits_passwords(monkeypatch):
    payload = {
        "count": 1234,
        "lines": [
            "victim@example.com:SuperSecret123",
            "victim.alt@gmail.com:hunter2",
        ],
    }
    monkeypatch.setattr(pn_mod, "http_get", lambda *a, **k: _FakeResp(payload))
    adapter = ProxyNovaAdapter()
    _ctx(adapter, "victim@example.com", "email")

    units = adapter.parse(adapter.run("victim@example.com"))
    assert len(units) == 1
    unit = units[0]
    assert unit.result_type == "breach_hit"
    assert unit.result_value == "proxynova-combolist"
    notes = unit.notes or ""
    # The plaintext secrets must never appear anywhere in the evidence.
    assert "SuperSecret123" not in notes
    assert "hunter2" not in notes
    assert "passwords=masked/omitted" in notes
    assert "combolist_entries=1234" in notes
    # The associated identifier is surfaced, but with its local-part masked.
    assert "associated_identifiers=" in notes
    assert "@gmail.com" in notes
    assert "victim.alt" not in notes


def test_proxynova_skips_phone_seed(monkeypatch):
    monkeypatch.setattr(pn_mod, "http_get", lambda *a, **k: _FakeResp({"lines": []}))
    adapter = ProxyNovaAdapter()
    _ctx(adapter, "+15551234567", "phone")
    assert adapter.run("+15551234567") == []


# --------------------------------------------------------------------------- #
# Forum / blog / comment sweep
# --------------------------------------------------------------------------- #
def test_forum_sweep_parses_dork_hits(monkeypatch):
    hits = [
        {"url": "https://www.reddit.com/user/target", "title": "target on Reddit"},
        {"url": "https://medium.com/@target", "title": "target on Medium"},
    ]
    monkeypatch.setattr(fs_mod, "ddg_search", lambda *a, **k: hits)
    adapter = ForumSweepAdapter()
    _ctx(adapter, "target", "username")

    units = adapter.parse(adapter.run("target"))
    assert units, "expected forum/blog dork hits"
    assert all(u.result_type == "dork_hit" for u in units)
    platforms = {u.source_platform for u in units}
    assert "reddit.com" in platforms
    assert "medium.com" in platforms
