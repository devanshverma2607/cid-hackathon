"""Comprehensive per-tool liveness probe (throwaway).

Runs every registered adapter's execute() against a category-appropriate seed
inside its OWN subprocess with a hard timeout, so a hung/slow tool can never
block the rest. Classifies each tool as DATA / EMPTY / UNAVAIL / TIMEOUT / ERROR
and prints a single summary table plus a JSON blob for machine parsing.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import sys
import time
import traceback
import uuid

# ---- seeds per category -----------------------------------------------------
USERNAME_SEED = "torvalds"
EMAIL_SEED = "john.smith@gmail.com"
PHONE_SEED = "+14155552671"
DOMAIN_SEED = "github.com"

# Per-tool hard timeout (seconds). Known-slow tools get more headroom.
DEFAULT_TIMEOUT = 75
SLOW = {"nexfil": 150, "wayback_urls": 200, "waybackurls": 200, "dnstwist": 150,
        "finalrecon": 150, "maigret": 120, "enola": 200, "detectdee": 200,
        "social_analyzer": 150, "theharvester": 120, "sublist3r": 120}


def _import_adapters():
    from worker_python.adapters import fallback_chain as fc
    return fc


def _adapter_specs(fc):
    """Yield (label, adapter_cls, seed, seed_type) for every unique adapter."""
    specs = []
    seen = set()

    def add(cls, seed, seed_type):
        if cls in seen:
            return
        seen.add(cls)
        specs.append((cls().name(), cls, seed, seed_type))

    # Chains
    chain_seed = {
        "username_tier1": (USERNAME_SEED, "username"),
        "username_tier2": (USERNAME_SEED, "username"),
        "email_tier1": (EMAIL_SEED, "email"),
        "email_tier2": (EMAIL_SEED, "email"),
        "phone_tier1": (PHONE_SEED, "phone"),
        "passive_recon": (USERNAME_SEED, "username"),
    }
    for chain, adapters in fc.FallbackChainManager.chains.items():
        seed, st = chain_seed[chain]
        for cls in adapters:
            add(cls, seed, st)

    # Platform map (username seed, except domain -> domain seed)
    for platform, adapters in fc.FallbackChainManager.platform_map.items():
        seed = DOMAIN_SEED if platform == "domain" else USERNAME_SEED
        st = "domain" if platform == "domain" else "username"
        for cls in adapters:
            add(cls, seed, st)

    return specs


def _run_one(name, cls, seed, seed_type, q):
    try:
        adapter = cls()
        cid, rid = uuid.uuid4(), uuid.uuid4()
        t0 = time.monotonic()
        try:
            healthy = adapter.health_check()
        except Exception:
            healthy = None
        units = adapter.execute(seed, cid, rid, "probe", seed_type)
        dt = time.monotonic() - t0
        positives = [u for u in units if u.result_type not in ("unavailable", "blocked")]
        unavail = [u for u in units if u.result_type in ("unavailable", "blocked")]
        if positives:
            status = "DATA"
        elif unavail:
            status = "UNAVAIL"
        else:
            status = "EMPTY"
        sample = []
        for u in positives[:3]:
            sample.append(f"{u.result_type}:{u.source_platform}:{(u.result_value or '')[:48]}")
        q.put({"name": name, "status": status, "healthy": healthy,
               "positives": len(positives), "unavail": len(unavail),
               "secs": round(dt, 1), "sample": sample,
               "reason": (unavail[0].notes if unavail and hasattr(unavail[0], "notes") else "")})
    except Exception:
        q.put({"name": name, "status": "ERROR", "healthy": None,
               "positives": 0, "unavail": 0, "secs": 0, "sample": [],
               "reason": traceback.format_exc()[-300:]})


def main():
    fc = _import_adapters()
    specs = _adapter_specs(fc)

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--list" in sys.argv:
        for name, _cls, seed, st in specs:
            print(f"{name:24} seed={seed} ({st})")
        print(f"\n# {len(specs)} tools")
        return
    if args:
        wanted = set(args)
        specs = [s for s in specs if s[0] in wanted]

    # Append (not overwrite) so batch runs accumulate into one results file.
    import os
    existing = []
    if "--append" in sys.argv and os.path.exists("/tmp/liveness_results.json"):
        try:
            existing = json.load(open("/tmp/liveness_results.json"))
        except Exception:
            existing = []
    results = list(existing)
    print(f"# Probing {len(specs)} tools (have {len(existing)} prior)\n", flush=True)
    for name, cls, seed, seed_type in specs:
        timeout = SLOW.get(name, DEFAULT_TIMEOUT)
        q = mp.Queue()
        p = mp.Process(target=_run_one, args=(name, cls, seed, seed_type, q))
        p.start()
        p.join(timeout)
        if p.is_alive():
            p.terminate()
            p.join(5)
            res = {"name": name, "status": "TIMEOUT", "healthy": None,
                   "positives": 0, "unavail": 0, "secs": timeout, "sample": [],
                   "reason": f"exceeded {timeout}s"}
        else:
            try:
                res = q.get_nowait()
            except Exception:
                res = {"name": name, "status": "ERROR", "healthy": None,
                       "positives": 0, "unavail": 0, "secs": 0, "sample": [],
                       "reason": "no result on queue (crashed)"}
        results.append(res)
        print(f"{res['status']:8} {name:20} pos={res['positives']:<3} "
              f"unavail={res['unavail']:<2} {res['secs']:>5}s "
              f"{('| ' + '; '.join(res['sample'])) if res['sample'] else ''}",
              flush=True)
        if res["status"] in ("ERROR", "UNAVAIL") and res["reason"]:
            print(f"         reason: {res['reason'].strip()[:200]}", flush=True)
        # Incremental persist so progress survives a terminal kill.
        with open("/tmp/liveness_results.json", "w") as fh:
            json.dump(results, fh)

    # Summary
    from collections import Counter
    by_status = Counter(r["status"] for r in results)
    print("\n# SUMMARY:", dict(by_status), flush=True)
    print("\n# JSON_START")
    print(json.dumps(results))
    print("# JSON_END")

    # Persist to a file inside the container so results survive terminal death.
    with open("/tmp/liveness_results.json", "w") as fh:
        json.dump(results, fh)
    print("# WROTE /tmp/liveness_results.json", flush=True)


if __name__ == "__main__":
    main()
