# Detailed Continuation Prompt for Claude Opus 4.6 (Antigravity)

## MISSION
Complete the deployment of **Identifier-Based SOCMINT Collection** feature: validate, test, and restart the 4 keyed adapters (IntelX, AbstractPhone, VirusTotal, Shodan) that were built and wired on 2026-06-19. Current state: **70% complete** — code written + wired, NOT YET validated/tested/deployed.

---

## CONTEXT: SOCMINT Codebase Overview

### Stack
- **Broker**: Celery 5.4.0 (Redis 7 backend) with acks_late=True, prefetch=1, task_reject_on_worker_lost=True (recovery hardening applied 2026-06-13)
- **Databases**: PostgreSQL 15 + pgvector, Neo4j 5 + GDS plugin, MinIO (artifacts)
- **Workers**: 2 Celery workers on single broker:
  - `worker_python` (concurrency=4, queue="celery") — Python adapters Tier 1-4, orchestration
  - `worker_go` (concurrency=2, queue="go") — compiled Go binaries (enola, gowitness, etc.)
  - **CRITICAL**: Fixed 2026-06-13 — workers were cannibalizing each other's tasks until queue isolation + task_routes were added (ARCHITECTURE.md §6.3)
- **API**: FastAPI (8000, NO --reload, must restart on code edit)
- **Dashboard**: Streamlit (8501, auto-reload)
- **Orchestration**: docker-compose.yml with 10 services

### Adapter Pattern (base.py)
All adapters inherit from ToolAdapter ABC:
```python
class ToolAdapter(ABC):
    def health_check(self) -> bool: ...  # Returns False if missing key/prereq
    def run(self, seed: str) -> list[dict]: ...  # Returns raw results
    def parse(self, raw: dict) -> EvidenceUnit: ...  # Converts to evidence schema
    def name(self) -> str: ...  # Unique name
    def execute(seed) -> EvidenceUnit|None: ...  # Calls health_check→run→parse; on failure returns {'result_type': 'unavailable', 'notes': 'reason'}
```

**Graceful Degradation**: Tool failure → single unavailable EvidenceUnit, never raises. Missing API key → health_check False → skipped with unavailable marker.

### Fallback Chains (worker_python/adapters/fallback_chain.py)
Dictionary mapping seed_type + tier to list of adapter classes:
```python
chains = {
    'email_tier1': [PhoneEnrichAdapter, ...],  # Fast, local
    'email_tier2': [H8MailAdapter, SocialScanAdapter, ..., XposedOrNotAdapter, HudsonRockAdapter, ProxyNovaAdapter, IntelXAdapter],  # Deep enum
    'username_tier1': [...],
    'username_tier2': [SherlockAdapter, ..., HudsonRockAdapter, ProxyNovaAdapter, IntelXAdapter],
    'phone_tier1': [PhoneEnrichAdapter, AbstractPhoneAdapter, ...],  # ← NEW
    'domain_tier2': [...],
    'domain_tier4': [VirusTotalAdapter, ShodanIntelAdapter, ...],  # ← NEW (triggered after correlation confirms domain)
    'passive_recon': [ForumSweepAdapter, ...],  # Tier 3, Tor
    ...
}
```

### Pipeline Tiers & Orchestration (worker_python/tasks/_pipeline.py)
- **Tier 1**: Fast, local, synchronous (phone_enrich, identity_extract)
- **Tier 2**: Deep enumeration, ~1-10s per tool (sherlock, h8mail, xposedornot, hudsonrock, proxynova, intelx, etc.) → dispatched in Celery chord header
- **Tier 3**: Passive, slow, Tor (DDG dorks, forum_sweep, archive.org) → background task
- **Tier 4**: Enrichment, triggered ONLY after correlation finds hits (VirusTotal, Shodan, finalrecon, dnstwist, etc.) → dispatched after aggregate_results
- **Correlation**: After Tier 2 completes, aggregate_results runs, correlation_2.2 engine scores hits, then Tier 4 fires
- **Cooldowns** (COOLDOWNS dict in _pipeline.py): per-tool sleep between executions (e.g., xposedornot: 1s, virustotal: 15s, shodan: 2s) to respect API rate limits
- **Recovery**: Watchdog finalize_correlation (countdown=540) re-runs aggregate if chord header dropped; beat service (worker_beat) polls every 120s to recover stuck runs

### Evidence & Correlation
- **EvidenceUnit** schema (api/models/evidence.py): case_id, seed_value, seed_type, source_platform, source_tier, result_type, result_value, notes, platform_enrichment{}, raw_data
- **Correlation Engine** (correlation_2.2): Pairwise platform scoring on username/email/phone/breach matches; W_BREACH_REUSE=18 (breach hit result_value → breach_sources set for intersection); MIN_SIGNALS=2 (discard single-signal hits)
- **Dedup Key**: UNIQUE(case_id, source_platform, result_value, seed_value) — upserts prevent duplicates

### Deploy & Restart Rules
- **worker_python edit** → `docker compose restart worker_python worker_beat api` (NOT --reload; bind-mounts live, code reloaded on restart)
- **api edit** → same restart (code not reloaded in container, must restart)
- **dashboard edit** → auto-reloads (runs with --logger.level=warning)
- **No Dockerfile changes needed** for new adapters (httpx already in requirements.txt, no new CLI deps)

---

## WHAT HAS BEEN DONE (2026-06-14 & 2026-06-19)

### Phase 1: Keyless Adapters (2026-06-14) ✅ DEPLOYED
4 adapters built, tested (8 tests pass), wired, validated (F1 0.952), deployed:

1. **worker_python/adapters/email/xposedornot.py** — XposedOrNotAdapter
   - API: `GET api.xposedornot.com/v1/breach-analytics?email={email}` (fallback to /v1/check-email)
   - Tier 2, use_tor=False (Cloudflare blocks Tor exits)
   - Health: Always True (no key required)
   - Parse: Breach names + exposed_data types → result_value=breach_name; flags "association=email↔username" in notes if Usernames in exposed_data
   - Cooldown: 1s
   - Dependencies: httpx, _net.http_get

2. **worker_python/adapters/email/hudsonrock.py** — HudsonRockAdapter
   - API: `GET cavalier.hudsonrock.com/api/json/v2/osint-tools/search-by-{email|username|domain}?query={seed}`
   - Tier 2, use_tor=False
   - Health: Validates seed is email/username/domain regex
   - Parse: Stealer array (date_compromised, os, total_user_services, top_logins) → result_value=f"infostealer:{date}"; notes include user_services count + API-masked linked_logins
   - Cooldown: 2s
   - Dependencies: httpx, _net.http_get, _net.clean_domain

3. **worker_python/adapters/email/proxynova.py** — ProxyNovaAdapter
   - API: `GET api.proxynova.com/comb?query={seed}&limit=25` → "account:secret" lines
   - Tier 2, use_tor=False
   - Health: Seed is email or username (skips phone)
   - Parse: **CRITICAL SECURITY**: Splits on ":", drops secret half, masks identifier via _mask_identifier (john.doe@gmail.com → j*******@gmail.com). Never stores plaintext credentials.
   - Result: result_value="proxynova-combolist", notes="passwords=masked/omitted" + masked identifiers
   - Cooldown: 2s
   - Dependencies: httpx, _net.http_get

4. **worker_python/adapters/passive/forum_sweep.py** — ForumSweepAdapter
   - API: Keyless DDG site: dorks + Mojeek (Tor fallback) for forums/blogs/comments
   - Tier 3, use_tor=True (Tor routed via socks5://tor:9050)
   - Health: Always True
   - Parse: DDG results → dork_hit, extract platform from URL, result_value=URL
   - Dorks: seed_type-specific (USERNAME_DORKS, EMAIL_DORKS, PHONE_DORKS) scoped to reddit/HN/quora/stackexchange/medium/substack/dev.to/wordpress/disqus, max 6 templates, 10 results each
   - Cooldown: 30s
   - Dependencies: _net.ddg_search, _net.select_dorks

**Wiring (Phase 1)**:
- fallback_chain.py: email_tier2 += [XposedOrNotAdapter, HudsonRockAdapter, ProxyNovaAdapter]; username_tier2 += [HudsonRockAdapter, ProxyNovaAdapter]; passive_recon += [ForumSweepAdapter]
- api/routers/pipeline.py: TIER_TOOLS[2] += ['xposedornot', 'hudsonrock', 'proxynova']; TIER_TOOLS[3] += ['forum_sweep']; _PASSIVE_TOOLS += 'forum_sweep'
- worker_python/tasks/_pipeline.py: COOLDOWNS += {'xposedornot': 1, 'hudsonrock': 2, 'proxynova': 2, 'forum_sweep': 30}

**Test** (tests/test_collection_adapters.py):
- 8 tests, all mocked (no real HTTP)
- Verifies: breach name parsing, username exposure flagging, Hudson Rock stealer parsing, **ProxyNova password masking** (asserts neither "SuperSecret123" nor "hunter2" in notes; asserts "passwords=masked/omitted" present), forum dork parsing
- Test result: 8/8 PASS

**Deployment**: Full suite 56/56 pass; validation F1 0.952 9/9 invariants PASS; compileall clean; docker compose restart worker_python worker_beat api (verified in-container chains loaded).

---

### Phase 2: Keyed Adapters (2026-06-19) ✅ BUILT + WIRED, ⏳ NOT YET TESTED/DEPLOYED

User provided 4 API keys (now in .env):
```
INTELX_API_KEY=db3a3469-3faf-4e80-8ac5-0d0502f85e07
ABSTRACTAPI_PHONE_KEY=427c0944e3d24db1966e66ea25fdcbaf
VIRUSTOTAL_API_KEY=eaf2fcc48faf9f06ef9b13dd4cf73b9df4c33a5a472b7b42de8041b0c16714c6
SHODAN_API_KEY=1TrAp4AO2t0zB6m0VZCCczasjI9Turtv
```

1. **worker_python/adapters/email/intelx.py** — IntelXAdapter
   - API: 2-phase: POST {term, buckets, maxresults} → response.id, then GET /result?id (poll up to 4 times × 1.5s sleep)
   - Base URLs: tries 2.intelx.io, fallback free.intelx.io (override via INTELX_BASE_URL env)
   - Tier 2, use_tor=False
   - Health: `bool(os.environ.get("INTELX_API_KEY"))` → unhealthy if blank
   - Parse: maxresults=10, terminates search on completion (frees server slot); result_value=record_name[:200] or f"intelx:{bucket}", source_tier=3; notes include bucket, date, record type
   - Cooldown: 2s
   - Dependencies: httpx, time.sleep

2. **worker_python/adapters/phone/abstract_phone.py** — AbstractPhoneAdapter
   - API: `GET phonevalidation.abstractapi.com/v1?api_key={key}&phone={E164}`
   - Tier 1, use_tor=False
   - Health: `bool(os.environ.get("ABSTRACTAPI_PHONE_KEY"))` → unhealthy if blank
   - Parse: Normalizes seed to E.164 via libphonenumber (fallback DEFAULT_PHONE_REGION); only proceeds if response.valid=True; extracts carrier, line_type, country, location
   - Result: result_type=phone_intel, source_tier=1, result_value=E164, platform_enrichment={carrier, line_type, country, location}
   - Complements offline phone_enrich with live carrier lookup
   - Cooldown: 1s
   - Dependencies: httpx, phonenumbers

3. **worker_python/adapters/platform/virustotal.py** — VirusTotalAdapter
   - API: `GET virustotal.com/api/v3/domains/{domain}` (x-apikey header)
   - Tier 4, use_tor=False
   - Health: `bool(os.environ.get("VIRUSTOTAL_API_KEY"))` → unhealthy if blank
   - Parse: last_dns_records (A/AAAA/MX/etc, max 25), categories, reputation, analysis_stats (malicious/suspicious counts), registrar, creation_date, tags
   - Result: result_type=domain_hit, source_tier=2, result_value=domain, platform_enrichment={full parsed summary}, notes include reputation + analysis verdict + categories
   - Cooldown: 15s (free tier 4 req/min)
   - Dependencies: httpx, _net.clean_domain, _net.http_get

4. **worker_python/adapters/platform/shodan_intel.py** — ShodanIntelAdapter
   - API: `GET api.shodan.io/dns/domain/{domain}?key={key}`
   - Tier 4, use_tor=False
   - Health: `bool(os.environ.get("SHODAN_API_KEY"))` → unhealthy if blank
   - Parse: subdomains array (max 80), tags, data records → returns summary domain_hit + per-subdomain dork_hit (if subdomain extracted)
   - Result: domain_hit (summary) + dork_hit per subdomain, source_tier=2-3, result_value=domain or FQDN
   - Cooldown: 2s
   - Dependencies: httpx, _net.clean_domain, _net.http_get

**Wiring (Phase 2)**:
- fallback_chain.py: email_tier2 += [IntelXAdapter]; phone_tier1 += [AbstractPhoneAdapter]; domain matrix += {virustotal: VirusTotalAdapter, shodan: ShodanIntelAdapter}
- api/routers/pipeline.py: TIER_TOOLS[1] += ['abstractapi_phone']; TIER_TOOLS[2] += ['intelx']; TIER_TOOLS[4] += ['virustotal', 'shodan']; _EMAIL_TOOLS += 'intelx'; _PHONE_TOOLS += 'abstractapi_phone'; domain matrix updated
- worker_python/tasks/_pipeline.py: COOLDOWNS += {'intelx': 2, 'abstractapi_phone': 1, 'virustotal': 15, 'shodan': 2}
- .env/.env.example: Added 12 key slots (organized by category: Breach, Email, Phone, Domain, Platform), labeled "provide key then ask to wire adapter"

**Status**: Code complete, compiles, NOT YET tested or deployed.

---

## IMMEDIATE TASKS (Next 30-60 minutes)

### 1. Validate Compilation ✅
**Commands**:
```bash
cd d:\cid hackathon\socmint
python -m compileall -q worker_python/adapters/email/intelx.py worker_python/adapters/phone/abstract_phone.py worker_python/adapters/platform/virustotal.py worker_python/adapters/platform/shodan_intel.py worker_python/adapters/fallback_chain.py api/routers/pipeline.py worker_python/tasks/_pipeline.py
```
**Expected**: Exit code 0 (no output = clean)

**If errors**:
- Use `get_errors` tool to identify issues
- Check import paths, syntax, environment variable access
- Fix and re-compile

---

### 2. Run Full Test Suite ✅
**Commands**:
```bash
cd d:\cid hackathon\socmint
python -m pytest tests/test_collection_adapters.py -v
python -m pytest tests/ -q  # Full suite
```
**Expected**: 
- test_collection_adapters.py: 8 pass (keyless tests, unchanged)
- Full suite: 48+ pass (existing tests, no new keyed tests yet)
- No failures

**Note**: Keyed adapters do NOT have tests yet (mocking httpx with API key gating is complex; can be added later if needed).

---

### 3. Run Validation Gate ✅
**Commands**:
```bash
cd d:\cid hackathon\socmint
python -m api.validation.run
```
**Expected**: 
- F1 score >= 0.80 (should be 0.952 unchanged, as no changes to correlation/evidence schema)
- All 9 invariants PASS (audit_log, dedup key, min_signals, etc.)
- No errors

**What this does**: Offline test suite (runs synthetic OSINT on predefined seeding patterns, evaluates precision/recall, checks DB invariants).

---

### 4. Check for Import/Syntax Errors ✅
**Commands**:
```bash
# Import test
python -c "from worker_python.adapters.fallback_chain import FallbackChainManager; print('✓ Chains load OK')"
python -c "from api.routers.pipeline import TIER_TOOLS; print('✓ Pipeline registry OK')"
python -c "from worker_python.adapters.email.intelx import IntelXAdapter; print('✓ IntelX loads OK')"
python -c "from worker_python.adapters.phone.abstract_phone import AbstractPhoneAdapter; print('✓ AbstractPhone loads OK')"
python -c "from worker_python.adapters.platform.virustotal import VirusTotalAdapter; print('✓ VT loads OK')"
python -c "from worker_python.adapters.platform.shodan_intel import ShodanIntelAdapter; print('✓ Shodan loads OK')"
```
**Expected**: All succeed (no import errors, no syntax errors)

---

### 5. Restart Services ⏳
**Commands**:
```bash
cd d:\cid hackathon
docker compose restart worker_python worker_beat api
# Wait ~30s for containers to start
docker compose ps
```
**Expected**: All 3 services show "Up" status

---

### 6. Verify In-Container Adapter Registration ⏳
**Commands**:
```bash
# Check email_tier2 chain includes all 4 new breach adapters
docker compose exec -T worker_python python -c "from worker_python.adapters.fallback_chain import FallbackChainManager as F; adapters = [c().name() for c in F.chains['email_tier2']]; print('email_tier2 tail:', adapters[-4:]); assert 'xposedornot' in adapters and 'hudsonrock' in adapters and 'proxynova' in adapters and 'intelx' in adapters, 'Missing adapters!'"

# Check phone_tier1 chain includes AbstractPhoneAdapter
docker compose exec -T worker_python python -c "from worker_python.adapters.fallback_chain import FallbackChainManager as F; adapters = [c().name() for c in F.chains['phone_tier1']]; print('phone_tier1:', adapters); assert 'abstractapi_phone' in adapters, 'AbstractPhone missing!'"

# Check domain Tier 4 includes VT + Shodan
docker compose exec -T worker_python python -c "from worker_python.adapters.fallback_chain import FallbackChainManager as F; vt = F.chains.get('platform_tier4', {}).get('virustotal'); shodan = F.chains.get('platform_tier4', {}).get('shodan'); print(f'VT: {vt}, Shodan: {shodan}')"

# Check API registry
docker compose exec -T api python -c "from api.routers.pipeline import TIER_TOOLS; print('Tier 1:', TIER_TOOLS[1]); print('Tier 2:', TIER_TOOLS[2][-5:]); print('Tier 4:', TIER_TOOLS[4])"
```
**Expected**: All adapters loaded, chains contain new tools, API registry updated

---

### 7. Test Live Scan (Optional but Recommended) ⏳
Run a fresh case with email seed, watch pipeline status:
- Dashboard: http://localhost:8501 → Case Intake → Create case with example email + one more seed type (e.g., phone or username)
- Go to Pipeline Status page, watch tools execute
- Should see: intelx in email_tier2 chain running after h8mail/socialscan; abstractapi_phone in phone_tier1; virustotal/shodan in tier4 (after correlation)
- **Success criteria**: No tool errors, all evidence units have valid source_platform/result_type

---

## KEY COMMANDS & DEBUGGING

### If Compilation Fails
```bash
python -m py_compile worker_python/adapters/email/intelx.py  # More verbose error
```

### If Tests Fail
```bash
python -m pytest tests/test_collection_adapters.py::test_xposedornot_parses_named_breaches_and_flags_username_exposure -v
# Replace test name with failing test
```

### If Docker Restart Fails
```bash
docker compose logs worker_python | tail -50
docker compose logs api | tail -50
```

### If Health Checks Report Keyed Adapters Unhealthy
```bash
# Check if keys are in .env
cat socmint/.env | grep -E "INTELX|ABSTRACTAPI|VIRUSTOTAL|SHODAN"

# Test key loading in container
docker compose exec -T worker_python python -c "import os; print('INTELX:', bool(os.environ.get('INTELX_API_KEY')))"
```

---

## DEPLOYMENT SUMMARY

| Step | Command | Expected | Time |
|------|---------|----------|------|
| 1. Compile | `compileall -q *.py` | Exit 0 | 5m |
| 2. Test | `pytest -q` | 56+ pass | 10m |
| 3. Validate | `api.validation.run` | F1 ≥ 0.80 | 10m |
| 4. Restart | `docker compose restart ...` | Up | 1m |
| 5. Verify | `docker compose exec -T ...` | Chains loaded | 5m |
| 6. Live scan (opt) | Dashboard → case intake | No errors | 3m |
| **Total** | | | **34-50 min** |

---

## FILES TO REVIEW (Before Starting)

Core modified/new files (all in `d:\cid hackathon\socmint/`):

**New Adapters**:
- `worker_python/adapters/email/intelx.py` (171 lines)
- `worker_python/adapters/phone/abstract_phone.py` (91 lines)
- `worker_python/adapters/platform/virustotal.py` (111 lines)
- `worker_python/adapters/platform/shodan_intel.py` (106 lines)

**Wiring**:
- `worker_python/adapters/fallback_chain.py` (imports + 3 chains + 1 matrix)
- `api/routers/pipeline.py` (TIER_TOOLS + _EMAIL_TOOLS + _PHONE_TOOLS)
- `worker_python/tasks/_pipeline.py` (COOLDOWNS +4)
- `socmint/.env` (12 key slots, 4 filled)
- `socmint/.env.example` (template)

**Documentation**:
- `socmint/ARCHITECTURE.md` (updated tool catalogue + chains + cooldowns)
- `HANDOFF_2026_06_19.md` (this session's handoff; in repo root)

**Tests** (no new keyed tests):
- `tests/test_collection_adapters.py` (8 existing keyless tests, unchanged)

---

## KEY INVARIANTS & CONSTRAINTS

1. **Health Check Gating**: All 4 keyed adapters gate on `bool(os.environ.get("API_KEY"))` → returns False (unhealthy) if key not in environment → adapter skipped with unavailable marker. Never raises on missing key.

2. **Graceful Degradation**: Any adapter failure (HTTP timeout, JSON parse error, API error) → logs + returns single unavailable EvidenceUnit, chain continues. Never crashes the scan.

3. **Rate Limiting**: COOLDOWNS enforced per-tool; IntelX:2s, AbstractPhone:1s, VirusTotal:15s, Shodan:2s. Prevents API throttling.

4. **Dedup**: evidence_units has UNIQUE(case_id, source_platform, result_value, seed_value). Upserts prevent duplicates from retries.

5. **Correlation**: MIN_SIGNALS=2 mandatory; single-signal hits discarded. W_BREACH_REUSE=18 on breach_hit result_value → breach_sources intersection.

6. **No Dockerfile Changes**: httpx already in requirements.txt; no new Python deps or CLI tools added. Bind-mounts handle code sync.

7. **Recovery**: Watchdog finalize_correlation + beat service (poll every 120s) recover stuck scans. Task acks_late=True ensures redelivery on worker death.

---

## EXPECTED OUTCOMES

After completing all steps:

✅ Code compiles without errors
✅ All tests pass (56+/56+)
✅ Validation gate passes (F1 ≥ 0.80)
✅ Services running (Up status)
✅ Chains loaded in-container (adapters registered)
✅ API registry updated (TIER_TOOLS includes new tools)
✅ Live scan test confirms tools execute (optional verification)

**Result**: 8 new adapters (4 keyless + 4 keyed) live in production, gracefully degrading when keys absent, contributing to breach/leak/domain/phone enrichment.

---

## GOTCHAS & KNOWN ISSUES

1. **API Key Rotation**: INTELX, VIRUSTOTAL, SHODAN keys were exposed in chat (pasted by user for wiring). After hackathon, user should rotate these keys (revoke + regenerate in vendor dashboards).

2. **IntelX Free Tier Limited**: Free API key has daily credit/request caps. If tests hammer it, may hit limits. Solution: Add tests with mocked responses (not live HTTP).

3. **Shodan Subdomains**: ShodanIntelAdapter returns both domain_hit (summary) + dork_hit per subdomain. This may explode evidence unit count if domain has many subdomains. Limit is capped at 80 subdomains in adapter.

4. **VirusTotal Reputation**: Requires API key with appropriate permissions. Free tier has limited requests (4/min). Cooldown set to 15s (4 req/min) to respect this.

5. **Docker Clock Skew**: VM clock ~1 day behind host (cosmetic, shared by all containers). Do NOT force-fix; timestamps are consistent within containers.

6. **AbstractPhone E164 Normalization**: Uses libphonenumber (already in requirements). Requires DEFAULT_PHONE_REGION env var or fallback (currently 'US'). Non-standard phone formats may fail gracefully (returns unavailable).

---

## NEXT SESSION HANDOFF (If Needed)

If you run out of time or tokens before completing all steps:

1. **Save progress**: Note which steps completed (compile → test → validate → etc.)
2. **Update HANDOFF_2026_06_19.md**: Mark completed items with ✅, note any errors
3. **Tag the state**: "Reached step 5 (restart), all tests pass, awaiting in-container verify"
4. **Pass to next agent**: Link this prompt + updated handoff

---

## REFERENCES

- **SOCMINT Codebase**: d:\cid hackathon\socmint/
- **Repo Memory**: d:\cid hackathon\socmint → /memories/repo/socmint.md
- **Session Notes**: This prompt + HANDOFF_2026_06_19.md
- **Architecture Spec**: socmint/ARCHITECTURE.md (§6.3 on worker queue isolation, §5.3 on cooldowns, tool catalogue)
- **API Keys**: socmint/.env (user-filled, gitignored)

---

**READY TO START?** Begin with **Step 1: Validate Compilation**, proceed in order, report each result before moving to the next step.
