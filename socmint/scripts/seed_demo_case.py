#!/usr/bin/env python3
"""Seed a demo case through the live API for hackathon demonstrations.

Usage:
    python scripts/seed_demo_case.py [--api http://localhost:8000] [--seed alice]
"""
from __future__ import annotations

import argparse
import sys

import requests


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed a SOCMINT demo case.")
    parser.add_argument("--api", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--seed", default="johndoe", help="Seed value")
    parser.add_argument("--seed-type", default="username", choices=["username", "email", "phone"])
    args = parser.parse_args()

    payload = {
        "authority_id": "DEMO-AUTH-001",
        "agency_id": "DEMO-AGENCY",
        "analyst_id": "demo-analyst",
        "supervisor_approval": True,
        "purpose_statement": "Demonstration case for SOCMINT pipeline validation and review.",
        "target_category": "research",
        "jurisdiction": "IN",
        "retention_period": 30,
        "seed_type": args.seed_type,
        "seed_value": args.seed,
    }

    url = f"{args.api}/api/v1/cases/create"
    print(f"POST {url}\n  seed={args.seed_type}:{args.seed}")
    try:
        resp = requests.post(url, json=payload, timeout=30)
    except requests.RequestException as exc:
        print(f"ERROR: could not reach API: {exc}", file=sys.stderr)
        return 1

    if resp.status_code in (200, 201):
        data = resp.json()
        print(f"Case created: {data.get('case_id')}")
        print(f"Run ID:       {data.get('run_id')}")
        print(f"Pipeline:     {data.get('status')}")
        return 0

    print(f"ERROR ({resp.status_code}): {resp.text}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
