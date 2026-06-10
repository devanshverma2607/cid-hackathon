"""End-to-end pipeline validation (runs INSIDE worker_python; hits api:8000).

Validates the preservation-cap fix: tier2 username evidence should fully
persist AND the chord callback (correlation) should fire -> identity_links > 0.
"""
from __future__ import annotations

import time
import requests

API = "http://api:8000"


def hr(t):
    print(f"\n{'='*68}\n{t}\n{'='*68}", flush=True)


def main():
    payload = {
        "authority_id": "E2E-AUTH-002",
        "agency_id": "E2E-AGENCY",
        "analyst_id": "e2e-analyst",
        "supervisor_approval": True,
        "purpose_statement": "End-to-end validation of correlation firing and full tier2 evidence persistence.",
        "target_category": "research",
        "jurisdiction": "IN",
        "retention_period": 30,
        "seed_type": "username",
        "seed_value": "torvalds",
    }
    hr("1. CREATE CASE (username-only: torvalds)")
    r = requests.post(f"{API}/api/v1/cases/create", json=payload, timeout=60)
    print(f"HTTP {r.status_code}: {r.text[:200]}", flush=True)
    r.raise_for_status()
    case_id = r.json()["case_id"]
    print(f"case_id={case_id}", flush=True)

    hr("2. POLL UNTIL CORRELATION FIRES")
    correlated = False
    for i in range(140):  # up to ~11.5 min
        s = requests.get(f"{API}/api/v1/pipeline/status/{case_id}", timeout=30).json()
        st = requests.get(f"{API}/api/v1/reports/status/{case_id}", timeout=30).json()
        links = st.get("identity_links", 0)
        line = (f"  poll {i:03d}: state={s.get('state'):8} hits={str(s.get('total_hits')):>4} "
                f"tools_done={s.get('tools_done')}/{s.get('tools_total')} "
                f"high_links={s.get('high_confidence_links')} id_links={links}")
        print(line, flush=True)
        if links and int(links) > 0:
            correlated = True
            print("  -> CORRELATION CONFIRMED (identity_links > 0)", flush=True)
            break
        time.sleep(5)

    hr("3. PER-TOOL EVIDENCE (done tools)")
    s = requests.get(f"{API}/api/v1/pipeline/status/{case_id}", timeout=30).json()
    for tier in ("tier1", "tier2", "tier3", "tier4"):
        for t in s.get(tier, []):
            if t.get("status") == "done":
                print(f"   {tier} {t['tool']:18} hits={t['hits']}", flush=True)

    hr("4. PERSONA / INSIGHTS / REPORT")
    p = requests.get(f"{API}/api/v1/persona/{case_id}", timeout=30).json()
    print(f"persona: accounts={p.get('account_count')} personas={p.get('persona_count')} edges={len(p.get('edges', []))}", flush=True)
    ins = requests.get(f"{API}/api/v1/insights/{case_id}", timeout=30).json()
    print(f"insights: leads={len(ins.get('investigative_leads', []))} key_findings={len(ins.get('key_findings', []))}", flush=True)
    print(f"narrative: {str(ins.get('narrative',''))[:160]}", flush=True)
    rep = requests.post(f"{API}/api/v1/reports/generate/{case_id}", timeout=120)
    print(f"report generate: HTTP {rep.status_code} sha={str(rep.json().get('bundle_sha256','?'))[:16]}", flush=True)

    print(f"\nCORRELATED={correlated} CASE_ID={case_id}", flush=True)
    print("E2E_DONE", flush=True)


if __name__ == "__main__":
    main()
