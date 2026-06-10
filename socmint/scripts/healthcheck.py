#!/usr/bin/env python3
"""Check the health of every backing service via the API health endpoint.

Exit code 0 when healthy, 1 when degraded/unreachable. Suitable for CI smoke
tests or a `docker compose up` readiness probe.

Usage:
    python scripts/healthcheck.py [--api http://localhost:8000]
"""
from __future__ import annotations

import argparse
import sys

import requests


def main() -> int:
    parser = argparse.ArgumentParser(description="SOCMINT health check.")
    parser.add_argument("--api", default="http://localhost:8000", help="API base URL")
    args = parser.parse_args()

    url = f"{args.api}/api/v1/health"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        print(f"UNREACHABLE: {exc}", file=sys.stderr)
        return 1

    print(f"Overall: {data.get('status')}")
    for service, state in data.get("services", {}).items():
        mark = "OK " if state == "up" else "DOWN"
        print(f"  [{mark}] {service}: {state}")

    return 0 if data.get("status") == "healthy" else 1


if __name__ == "__main__":
    raise SystemExit(main())
