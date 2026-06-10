# SOCMINT Suspect Profiling System — GitHub Copilot Build Prompt
### For: Claude Opus 4.8 via GitHub Copilot | Reference: SOCMINT_PLAN_v2_0.txt

---

## WHO YOU ARE AND WHAT YOU ARE BUILDING

You are the lead engineer for the SOCMINT Suspect Profiling System, a hackathon-grade OSINT pipeline that accepts a single seed (username, email, or phone number) and automatically discovers, correlates, preserves, and reports linked identities across 1000+ platforms. The full specification lives in `SOCMINT_PLAN_v2_0.txt` in the root of this repository. Read it before writing any file. Every architectural decision, every module interface, every scoring weight, every tool name, every schema field, and every Docker container defined in that document is canonical. Do not invent alternatives to what is specified there.

The stack is entirely free and open-source. It runs locally via Docker Compose. There are no paid APIs, no cloud accounts, no licensing costs. The deliverable is a working demo that runs end-to-end from a single seed input to a downloadable PDF evidence report.

---

## REPOSITORY STRUCTURE TO CREATE

Before writing any implementation, scaffold the following directory tree exactly. Every folder and file listed here must exist when you are done.

```
socmint/
├── docker-compose.yml
├── .env.example
├── .gitignore
├── README.md
├── SOCMINT_PLAN_v2_0.txt          ← user places this here; do not overwrite
│
├── api/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py                    ← FastAPI app entry point
│   ├── routers/
│   │   ├── cases.py               ← /api/v1/cases endpoints
│   │   ├── pipeline.py            ← /api/v1/pipeline endpoints
│   │   ├── evidence.py            ← /api/v1/evidence endpoints
│   │   ├── graph.py               ← /api/v1/graph endpoints
│   │   └── reports.py             ← /api/v1/reports endpoints
│   ├── models/
│   │   ├── evidence.py            ← EvidenceUnit Pydantic model
│   │   ├── case.py                ← Case Pydantic model
│   │   └── identity_link.py       ← IdentityLink Pydantic model
│   ├── services/
│   │   ├── legal_gate.py          ← MODULE 1
│   │   ├── provenance.py          ← MODULE 0
│   │   ├── preservation.py        ← MODULE 4
│   │   ├── correlation.py         ← MODULE 6
│   │   ├── graph_builder.py       ← MODULE 7
│   │   ├── normaliser.py          ← MODULE 5
│   │   └── report_generator.py    ← MODULE 9
│   ├── db/
│   │   ├── postgres.py            ← SQLAlchemy engine + session
│   │   ├── neo4j.py               ← Neo4j driver wrapper
│   │   ├── minio_client.py        ← MinIO client wrapper
│   │   └── schema.sql             ← Full PostgreSQL DDL (all 4 tables)
│   └── config.py                  ← Settings from .env via pydantic-settings
│
├── worker_python/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── celery_app.py              ← Celery app definition
│   ├── tasks/
│   │   ├── tier1_tasks.py         ← Celery tasks for Tier 1 tools
│   │   ├── tier2_tasks.py         ← Celery tasks for Tier 2 tools
│   │   ├── tier3_tasks.py         ← Celery tasks for Tier 3 tools
│   │   └── tier4_tasks.py         ← Celery tasks for Tier 4 triggered tools
│   └── adapters/
│       ├── base.py                ← ToolAdapter abstract base class
│       ├── fallback_chain.py      ← FallbackChainManager
│       ├── username/
│       │   ├── blackbird.py
│       │   ├── whatsmyname.py
│       │   ├── sherlock.py
│       │   ├── maigret.py
│       │   ├── nexfil.py
│       │   ├── social_analyzer.py
│       │   └── tracer.py
│       ├── email/
│       │   ├── zehef.py
│       │   ├── socialscan.py
│       │   ├── hashtray.py
│       │   ├── holehe.py
│       │   ├── h8mail.py
│       │   ├── mailcat.py
│       │   ├── eyes.py
│       │   └── ghunt.py
│       ├── passive/
│       │   ├── dorks_eye.py
│       │   ├── dorksint.py
│       │   ├── wayback_urls.py
│       │   └── hunt_pastebin.py
│       └── platform/
│           ├── toutatis.py
│           ├── medor.py
│           ├── snapintel.py
│           ├── geogramint.py
│           ├── telegramsint.py
│           ├── tiktok_userdata.py
│           ├── mastosint.py
│           ├── osintssky.py
│           ├── osintchan.py
│           ├── proton_intel.py
│           ├── linkedin2username.py
│           ├── theharvester.py
│           └── finalrecon.py
│
├── worker_go/
│   ├── Dockerfile
│   ├── requirements.txt           ← Python wrapper deps (subprocess calls)
│   ├── celery_app.py
│   ├── tasks/
│   │   └── go_tasks.py            ← Celery tasks wrapping Go binaries
│   ├── adapters/
│   │   ├── enola.py
│   │   ├── detectdee.py
│   │   ├── mailsleuth.py
│   │   ├── email2whatsapp.py
│   │   ├── gowitness.py
│   │   └── githound.py
│   └── tools/go/                  ← Pre-compiled Go binaries go here
│       └── .gitkeep
│
├── dashboard/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app.py                     ← Streamlit app entry point (multi-page)
│   └── pages/
│       ├── 1_case_intake.py       ← Page 1: Legal Gate + seed input
│       ├── 2_pipeline_status.py   ← Page 2: Live tier/tool status
│       ├── 3_identity_graph.py    ← Page 3: NetworkX + Plotly graph
│       ├── 4_review_queue.py      ← Page 4: Confirm/Reject analyst queue
│       └── 5_report.py            ← Page 5: Download JSON + PDF
│
├── tests/
│   ├── test_legal_gate.py
│   ├── test_evidence_schema.py
│   ├── test_correlation_engine.py
│   ├── test_adapter_base.py
│   ├── test_preservation.py
│   └── test_report_generator.py
│
└── scripts/
    ├── setup_ghunt.sh             ← One-time Ghunt cookie auth helper
    ├── compile_go_tools.sh        ← Compiles all Go binaries to worker_go/tools/go/
    ├── seed_demo_case.py          ← Pre-seeds PostgreSQL with a cached demo run
    └── healthcheck.py             ← Verifies all 8 containers + all adapters
```

---

## STEP 1 — INFRASTRUCTURE FILES

### docker-compose.yml

Create the full `docker-compose.yml` exactly as specified in Section 12.1 of the plan. It must define 8 services: `postgres`, `neo4j`, `redis`, `minio`, `worker_python`, `worker_go`, `api`, `dashboard`. Use the exact image tags from the plan: `pgvector/pgvector:pg15`, `neo4j:5-community`, `redis:7-alpine`, `minio/minio`. Every service must declare `env_file: .env` and `depends_on` the correct upstream services. Add `restart: unless-stopped` to all services. Add healthcheck stanzas to postgres, neo4j, redis, and minio so dependent containers wait for them to be ready. The `worker_python` container must mount `./worker_python/adapters` and `./worker_python/tasks` as volumes for hot-reload during development. The `api` container must expose port 8000. The `dashboard` container must expose port 8501.

### .env.example

Create `.env.example` with every variable from Section 12.2 of the plan. Add comments above each variable explaining what it is. Include all variables: `POSTGRES_PASSWORD`, `NEO4J_PASSWORD`, `MINIO_USER`, `MINIO_PASSWORD`, `REDIS_URL`, `DATABASE_URL`, `NEO4J_URI`, `MINIO_ENDPOINT`, `H8MAIL_API_KEY`, `HIBP_API_KEY`, `INSTAGRAM_SESSION_ID`, `GHUNT_COOKIES_PATH`, `TOR_PROXY`. Also add `APP_ENV=development`, `LOG_LEVEL=INFO`, `API_HOST=0.0.0.0`, `API_PORT=8000`.

### .gitignore

Include: `.env`, `__pycache__/`, `*.pyc`, `*.pyo`, `.pytest_cache/`, `node_modules/`, `worker_go/tools/go/*` (except `.gitkeep`), `*.log`, `minio_data/`, `postgres_data/`, `neo4j_data/`, `redis_data/`, `*.sha256`, `cases/` (local output directory).

### README.md

Write a complete setup guide covering: prerequisites (Docker, Docker Compose, Go 1.21+, Python 3.11+), one-time setup steps (copy `.env.example` to `.env`, fill in values, run `compile_go_tools.sh`, run `setup_ghunt.sh`), how to start the stack (`docker compose up -d`), how to verify all services are healthy (run `scripts/healthcheck.py`), how to access each service (Streamlit at `localhost:8501`, FastAPI docs at `localhost:8000/docs`, Neo4j Browser at `localhost:7474`, MinIO console at `localhost:9001`), and how to run the tests.

---

## STEP 2 — DATABASE SCHEMA

### api/db/schema.sql

Implement the exact PostgreSQL DDL from Section 10.1 of the plan. All four tables: `cases`, `evidence_units`, `identity_links`, `audit_log`. Every column, type, constraint, and UNIQUE clause must match the spec exactly. After each table definition, add the correct indexes: on `cases(analyst_id)`, on `evidence_units(case_id, tool_tier)`, on `evidence_units(source_platform, result_value)`, on `identity_links(case_id, confidence_tier)`, on `audit_log(case_id, event_type)`. Add the `GRANT INSERT` comment on `audit_log` exactly as shown in the plan to signal append-only intent. Enable the `pgvector` extension at the top of the file. Add a `vector(384)` column named `bio_embedding` to `evidence_units` for pgvector semantic similarity (used by the correlation engine). Add a `CREATE UNIQUE INDEX` on `evidence_units` for the deduplication key `(case_id, source_platform, result_value, seed_value)`.

---

## STEP 3 — CORE MODELS (Pydantic)

### api/models/evidence.py

Define the `EvidenceUnit` Pydantic model with every field from MODULE 0 in Section 5 of the plan. Use `UUID` for IDs, `datetime` for timestamps, `Optional[dict]` for `signal_weights` and `platform_enrichment`, `Optional[str]` for nullable text fields. Add a `result_type` field constrained to the exact Literal values from the plan: `"account_found"`, `"email_registered"`, `"breach_hit"`, `"gravatar_hit"`, `"google_hit"`, `"whatsapp_hit"`, `"domain_hit"`, `"dork_hit"`, `"archive_hit"`, `"unavailable"`, `"blocked"`. Add a `tool_tier` field constrained to `Literal[1, 2, 3, 4]`. Add a `source_tier` field constrained to `Literal[1, 2, 3, 4]`.

### api/models/case.py

Define the `CaseCreate` and `Case` models. `CaseCreate` has all the mandatory Legal Gate fields from MODULE 1. `Case` extends `CaseCreate` with `case_id: UUID` and `created_at: datetime`. Add field-level validators: `seed_type` must be `Literal["username", "email", "phone"]`, `target_category` must be `Literal["cybercrime", "fraud", "harassment", "research"]`, `retention_period` must be a positive integer, `supervisor_approval` must be `True` (a `False` value fails the legal gate), `purpose_statement` must be at least 20 characters.

### api/models/identity_link.py

Define `IdentityLink` with every field from the `identity_links` table. Add `analyst_decision` constrained to `Optional[Literal["CONFIRMED", "REJECTED", "FLAG_UNCERTAIN"]]`. Add `confidence_tier` constrained to `Literal["HIGH", "MEDIUM", "LOW", "DISCARD"]`.

---

## STEP 4 — MODULE 0: EVIDENCE SCHEMA & PROVENANCE SERVICE

### api/services/provenance.py

Implement `ProvenanceService` as a class with the following methods, all exactly as described in Section 5 of the plan:

`create_evidence_unit(data: dict) -> EvidenceUnit` — validates all required fields, auto-generates `evidence_id` as a UUID4, sets `timestamp_collected` to `datetime.utcnow()`, returns a validated `EvidenceUnit`.

`validate_schema(unit: EvidenceUnit) -> bool` — runs Pydantic validation, checks that `seed_value` is not empty, checks that `tool_name` is not empty, returns `True` or raises `ValidationError`.

`write_to_db(unit: EvidenceUnit, session) -> UUID` — inserts into `evidence_units` table using an upsert on the `UNIQUE` constraint `(case_id, source_platform, result_value, seed_value)` — on conflict, update `timestamp_collected`, `snapshot_ref`, `snapshot_hash`, `wayback_ref`, `platform_enrichment`. Returns the `evidence_id`.

`compute_hash(artifact_bytes: bytes) -> str` — returns the SHA-256 hex digest of the given bytes.

`log_audit_event(case_id: UUID, run_id: UUID, event_type: str, actor_id: str, metadata: dict, session) -> None` — inserts a record into `audit_log`. This is an append-only operation. Never update or delete from this table.

`attach_enrichment(evidence_id: UUID, enrichment_data: dict, session) -> None` — updates only the `platform_enrichment` JSONB column on the row with the given `evidence_id`. Log an audit event of type `"ENRICHMENT_ATTACHED"`.

---

## STEP 5 — MODULE 1: LEGAL GATE & SEED MANAGER

### api/services/legal_gate.py

Implement `LegalGate` as a class with the following behaviour:

`validate(case_data: CaseCreate) -> tuple[bool, list[str]]` — checks every mandatory field. Returns `(True, [])` if all pass. Returns `(False, [list of missing or invalid field names])` if any fail. The checks: all 10 fields from Section 5 must be non-empty, `supervisor_approval` must be `True`, `purpose_statement` must be at least 20 characters, `seed_value` must pass format validation for its `seed_type`.

`normalise_seed(seed_type: str, seed_value: str) -> str` — applies the normalisation rules from MODULE 1: for username, lowercase and strip `@` prefix and whitespace; for email, lowercase and strip whitespace and validate format with a regex; for phone, attempt E.164 format using the `phonenumbers` library, fall back to stripping non-digit characters if the library fails.

`issue_case_id() -> UUID` — returns `uuid.uuid4()`.
`issue_run_id() -> UUID` — returns `uuid.uuid4()`.

### api/routers/cases.py

Implement the `POST /api/v1/cases/create` endpoint. It must: receive a `CaseCreate` body, call `LegalGate.validate()`, return HTTP 422 with error details if validation fails, normalise the seed, write the case to the `cases` table, write an audit log entry of type `"CASE_CREATED"`, issue a `run_id`, dispatch the pipeline Celery chord (Tier 1 + Tier 2 in parallel, Tier 3 in background), and return `{"case_id": ..., "run_id": ..., "status": "pipeline_started"}`.

---

## STEP 6 — MODULE 2: TOOLADAPTER FRAMEWORK

### worker_python/adapters/base.py

Implement the `ToolAdapter` abstract base class with the exact interface from MODULE 2 in the plan:

```
name()           → str         (abstract)
version()        → str         (abstract)
health_check()   → bool        (abstract — verify tool is installed and runnable)
run(seed: str)   → list[dict]  (abstract — returns raw parsed output dicts)
parse(raw: list[dict]) → list[EvidenceUnit]   (abstract)
get_proxy_tier() → int         (returns 1 for Tor, 2 for direct — default 2)
get_tool_tier()  → int         (abstract — returns 1, 2, 3, or 4)
```

Add a concrete `execute(seed: str, case_id: UUID, run_id: UUID, analyst_id: str) -> list[EvidenceUnit]` method on the base class that: calls `health_check()` and raises `ToolUnavailableError` if it returns `False`, calls `run(seed)`, calls `parse()`, attaches `case_id`, `run_id`, `analyst_id` to every `EvidenceUnit`, handles all exceptions by logging them and returning an empty list with a single `EvidenceUnit` of `result_type="unavailable"`, records execution time.

Add a `run_subprocess(cmd: list[str], timeout: int = 120, use_tor: bool = False) -> tuple[str, str, int]` helper method on the base class. It runs the command via `subprocess.run()`, captures stdout and stderr, respects timeout, and optionally routes through the Tor SOCKS5 proxy defined in the environment variable `TOR_PROXY` by setting `ALL_PROXY` in the subprocess environment.

### worker_python/adapters/fallback_chain.py

Implement `FallbackChainManager` with the full chain definitions from MODULE 2 of the plan:

```python
chains = {
    "username_tier1": [BlackbirdAdapter, WhatsMyNameAdapter],
    "username_tier2": [SherlockAdapter, MaigretAdapter, NexfilAdapter,
                       SocialAnalyzerAdapter, TracerAdapter,
                       EnolaAdapter, DetectDeeAdapter],
    "email_tier1":    [ZehefAdapter, SocialScanAdapter, HashtrayAdapter],
    "email_tier2":    [HoleheAdapter, H8mailAdapter, MailcatAdapter,
                       EyesAdapter, MailsleuthAdapter, GhuntAdapter,
                       Email2WhatsAppAdapter],
    "passive_recon":  [DorksEyeAdapter, DorksintAdapter,
                       WayBackURLsAdapter, HuntPastebinAdapter],
}
```

Implement `execute_chain(chain_name: str, seed_type: str, seed_value: str) -> list[EvidenceUnit]` — iterates through the chain, calls `adapter.execute()` on each, continues if a tool fails, logs `"TOOL_SKIPPED"` to the audit log for each failed tool, raises `ChainExhaustedError` with event type `"CHAIN_EXHAUSTED"` if all tools in the chain fail, returns deduplicated merged results.

Implement `trigger_platform_tools(platform: str, account_url: str, case_id: UUID, run_id: UUID) -> list[EvidenceUnit]` using the exact trigger matrix from Section 5 / MODULE 2. Every platform from the plan must be mapped to its adapter(s).

Implement `trigger_domain_tools(domain: str, case_id: UUID, run_id: UUID) -> list[EvidenceUnit]` — fires `TheHarvesterAdapter`, `FinalReconAdapter`, `WebdiverAdapter` in sequence.

---

## STEP 7 — ALL 35+ TOOL ADAPTERS

For every adapter listed in the repository structure above, implement a class that inherits from `ToolAdapter`. Each adapter must faithfully implement the install command, run command, parse logic, tier, proxy tier, fallback, and any notes from Section 11 of the plan. Every adapter must be self-contained — it installs its tool in the container's environment, wraps the CLI via `run_subprocess()`, and parses the output into `EvidenceUnit` objects.

The following adapters wrap Python pip-installable tools and must call the tool's CLI via subprocess (not import it as a library, to prevent dependency conflicts):

**Tier 1 — Username:** `BlackbirdAdapter` (runs `python blackbird.py -u {username} --json`), `WhatsMyNameAdapter` (runs `python3 whatsmyname.py -u {username} --output json`), `SocialScanAdapter` (runs `socialscan {email_or_username} --json`), `HashtrayAdapter` (runs `python hashtray.py {email_or_username}`).

**Tier 1 — Email:** `ZehefAdapter` (runs `python zehef.py {email}`, parses `[+]` lines from stdout).

**Tier 2 — Username:** `SherlockAdapter` (runs `sherlock {username} --json --output {tmpfile}`, parses JSON), `MaigretAdapter` (runs `maigret {username} --json --folderoutput {tmpdir}`, parses JSON report), `NexfilAdapter` (runs `nexfil -u {username}`, parses stdout for found/not-found), `SocialAnalyzerAdapter` (runs `python -m social_analyzer --username {username} --output json`), `TracerAdapter` (runs `python tracer.py -u {username}`, parses stdout).

**Tier 2 — Email:** `HoleheAdapter` (runs `holehe {email} --only-used --no-color`, parses `[+] site.com` lines), `H8mailAdapter` (runs `h8mail -t {email} -sk {api_keys_file}`, parses JSON output), `MailcatAdapter` (runs `mailcat {email}`, parses stdout), `EyesAdapter` (runs `python eyes.py {email}`, parses stdout), `GhuntAdapter` (runs `ghunt email {email} --json` in its isolated venv, parses JSON — Google profile, maps, YouTube, phone hints).

**Tier 3 — Passive:** `DorksEyeAdapter` (runs `python dorks_eye.py -q {seed} --output {tmpfile}`, Tor proxy, max 10 queries, 30s cooldown), `DorksintAdapter` (runs `dorksint -q {seed}`, Tor proxy, 30s cooldown), `WayBackURLsAdapter` (runs `waybackpy --url {url} --cdx`, parses CDX response), `HuntPastebinAdapter` (runs `python huntpastebin.py -q {seed}`, Tor proxy, max 5 queries).

**Tier 4 — Platform:** `ToutatisAdapter` (runs `toutatis -u {username} -s {session_id}`, parses JSON), `MedorAdapter` (runs `python medor.py {email}`, parses stdout), `SnapIntelAdapter` (runs `python snapintel.py -u {username}`), `GeogramintAdapter` (runs `python geogramint.py -p {phone_or_username}`), `TeleGramSintAdapter` (runs `python telegramsint.py -u {username}`), `TikTokUserDataAdapter` (runs `python tiktok_userdata.py -u {username}`, parses JSON), `MastOSINTAdapter`, `OSINTSkyAdapter`, `OSINTChanAdapter`, `ProtonIntelAdapter`, `LinkedIn2UsernameAdapter`, `TheHarvesterAdapter` (runs `theHarvester -d {domain} -b all -f {tmpfile}`, parses JSON), `FinalReconAdapter` (runs `python finalrecon.py --full {domain}`, parses JSON).

**Tier 2 — Go binaries (in worker_go):** `EnolaAdapter` (runs `./tools/go/enola {username} --json`), `DetectDeeAdapter` (runs `./tools/go/DetectDee find -u {username} --json`), `MailsleuthAdapter` (runs `./tools/go/mailsleuth -e {email} --json`), `Email2WhatsAppAdapter` (runs `./tools/go/email2whatsapp -e {email}`, Tor proxy), `GoWitnessAdapter` (runs `./tools/go/gowitness single --url {url} --screenshot-path {output_path}`), `GitHoundAdapter` (runs `echo {username} | ./tools/go/githound --dig --results-only`).

Every adapter's `parse()` method must map its tool's output to the `EvidenceUnit` schema. The `result_type` must be set correctly. Platform adapters that discover a profile must set `result_type="account_found"` and `result_value` to the profile URL. Email adapters that confirm a registration must set `result_type="email_registered"`. Breach tools must set `result_type="breach_hit"`. Archive tools must set `result_type="archive_hit"`. If a tool times out or returns an error, return a single `EvidenceUnit` with `result_type="unavailable"` and the error message in `notes`.

---

## STEP 8 — MODULE 3: TOOL EXECUTOR

### worker_python/tasks/tier1_tasks.py

Implement Celery tasks for Tier 1. Define `run_tier1_username_sweep(seed_value, case_id, run_id, analyst_id)` and `run_tier1_email_sweep(...)` as Celery tasks decorated with `@celery_app.task`. Each task instantiates the `FallbackChainManager` and calls `execute_chain("username_tier1", ...)` or `execute_chain("email_tier1", ...)`. After execution, call the `PreservationService` on every positive hit (`result_type` not `"unavailable"` and not `"blocked"`). Then write all `EvidenceUnit` objects to PostgreSQL via `ProvenanceService.write_to_db()`. Apply the rate limit cooldowns from MODULE 3 of the plan between tool runs using `time.sleep()`.

### worker_python/tasks/tier2_tasks.py

Same pattern for Tier 2 username and email chains. These tasks run in parallel via a Celery `group`. Define a `aggregate_results(results, case_id, run_id)` task that fires after the group completes (as the callback in a `chord`). It triggers the Correlation Engine.

### worker_python/tasks/tier3_tasks.py

Define `run_passive_recon(seed_value, seed_type, case_id, run_id, analyst_id)` as a background Celery task. It runs `DorksEyeAdapter`, `DorksintAdapter`, `WayBackURLsAdapter`, `HuntPastebinAdapter` with the rate limits from the plan. This task fires immediately when a case is created but does not block the Tier 1/2 chord.

### worker_python/tasks/tier4_tasks.py

Define `run_platform_enrichment(platform: str, account_url: str, username: str, case_id: UUID, run_id: UUID, analyst_id: str)` as a Celery task. It calls `FallbackChainManager.trigger_platform_tools(platform, account_url, ...)`. After platform enrichment, attach the results via `ProvenanceService.attach_enrichment()`. Screenshot via `GoWitnessAdapter`. Submit Wayback preservation.

---

## STEP 9 — MODULE 4: PRESERVATION SERVICE

### api/services/preservation.py

Implement `PreservationService` with this exact sequence from MODULE 4 of the plan:

`preserve(url: str, evidence_id: UUID, case_id: UUID) -> dict` — executes all 7 preservation steps in order:
1. Fetch full HTML of the URL using `httpx.AsyncClient` with a 30-second timeout and a realistic browser User-Agent.
2. Store the raw HTML bytes to MinIO at path `cases/{case_id}/{evidence_id}/raw.html` using the `MinIO` client.
3. Compute SHA-256 of the stored HTML bytes.
4. Call `GoWitnessAdapter.run(url, output_path=f"cases/{case_id}/{evidence_id}/screenshot.png")` and upload the screenshot to MinIO at `cases/{case_id}/{evidence_id}/screenshot.png`.
5. Submit a Wayback Machine save request via `https://web.archive.org/save/{url}` using an async HTTP GET with a 20-second timeout. Parse the `Content-Location` header from the response to get the `wayback_ref`. Handle HTTP 429 (rate limit) gracefully — log a warning, set `wayback_ref = None`, do not fail.
6. Call `WayBackURLsAdapter` to check if prior snapshots exist. If snapshots exist, call `WayBackPackAdapter` to download the most recent archived version and store it at `cases/{case_id}/{evidence_id}/wayback_snapshot.html` in MinIO.
7. Return `{"snapshot_ref": minio_path, "snapshot_hash": sha256_hex, "wayback_ref": archive_url_or_none, "preserved_at": utc_timestamp}`.

If any step fails, log the error and continue — never drop the evidence because preservation failed.

---

## STEP 10 — MODULE 5: DATA NORMALISER

### api/services/normaliser.py

Implement `DataNormaliser` with:

`normalise(raw_units: list[EvidenceUnit]) -> list[EvidenceUnit]` — applies all normalisation steps:
- Tag each unit with its `tool_tier` (integer 1-4) as reported by the adapter.
- Apply leet-speak normalisation to all `result_value` strings where `result_type="account_found"` using the mapping from Section 9.6: `0→o, 1→i, 3→e, 4→a, 5→s, 7→t, @→a`.
- Lowercase all usernames and email addresses.
- Apply evidence age decay: compute `evidence_age_days` as `(datetime.utcnow() - unit.timestamp_collected).days`. Compute `decay_factor` using the formula from Section 9.5. Apply it to `confidence_raw` if set.
- Deduplicate on key `(case_id, source_platform, result_value, seed_value)` — if a duplicate exists, keep the one with the higher `confidence_raw`.

`compute_bio_embedding(bio_text: str) -> list[float]` — uses `sentence_transformers.SentenceTransformer("all-MiniLM-L6-v2")` to compute a 384-dimensional embedding. Returns the vector as a Python list of floats for storage in pgvector.

---

## STEP 11 — MODULE 6: CORRELATION ENGINE

### api/services/correlation.py

This is the most complex module. Implement `CorrelationEngine` with exact signal weights, penalties, bonuses, decay, and rules from Section 9 of the plan. Do not approximate — every weight value is canonical.

`compute_confidence(units_a: list[EvidenceUnit], units_b: list[EvidenceUnit]) -> dict` — computes the confidence score between two groups of evidence units (representing two accounts being compared). Implements the full formula:

```
confidence_score = SUM(signal_weight * decay_factor)
                 - SUM(conflict_penalties)
                 [+ semantic_bonus: 0-10 if pgvector bio similarity available]
                 [+ gravatar_bonus: 0-8 if Hashtray confirms]
                 [+ google_bonus: 0-10 if Ghunt confirms]
                 [+ whatsapp_bonus: 0-7 if Email2WhatsApp confirms]
```

Implement every positive signal from Section 9.2 with the exact weight values:
- Identical username after leet-speak normalisation: weight 25
- Same email or `+alias` variation: weight 20
- Breach credential reuse from H8mail: weight 18 (with decay applied)
- Identical profile photo (pHash distance ≤ 8 using `imagehash`): weight 15
- Ghunt Google account confirmed: weight 12 (bonus, no decay)
- Bio similarity ≥ 80% via sentence-transformers cosine similarity: weight 10
- Overlapping linked external URLs (set intersection): weight 10
- Gravatar profile confirmed (Hashtray): weight 8 (bonus, no decay)
- WhatsApp linkage confirmed (Email2WhatsApp): weight 7 (bonus, no decay)
- Username Levenshtein distance ≤ 2: weight 5
- Dork hit confirming name + platform: weight 5 (with decay)
- Platform enrichment metadata match (bio, location, join date overlap): weight 5

Implement every conflict penalty from Section 9.3 with exact values:
- Account identified as bot: -15
- Timezone mismatch > 4 hours systematic: -10
- Primary language mismatch (via `langdetect`): -8
- Username on generic blacklist from Section 9.6: -8
- Account creation date impossible: -5
- Platform allows duplicate usernames (configurable per-platform flag in `config.yaml`): -5

Implement the 2-signal minimum rule from Section 9.4 as a hard gate. No identity link is written to Neo4j if fewer than 2 independent signals contributed to the score, regardless of the total.

Implement confidence thresholds from Section 9.4: 75–100 = HIGH, 50–74 = MEDIUM, 25–49 = LOW, 0–24 = DISCARD. DISCARD links are not written to Neo4j.

`run_full_correlation(case_id: UUID, session) -> list[IdentityLink]` — loads all `EvidenceUnit` rows for the case, groups by `source_platform`, computes pairwise identity link scores across platforms, enforces the 2-signal rule, writes HIGH and MEDIUM identity links to Neo4j, flags HIGH confidence links in the `identity_links` table for the analyst review queue, returns all computed links.

---

## STEP 12 — MODULE 7: GRAPH BUILDER

### api/services/graph_builder.py

Implement `GraphBuilder` that writes to and reads from Neo4j using the `neo4j` Python driver.

Define all node types from MODULE 7: `Identity`, `Account`, `Email`, `Username`, `Phone`, `Domain`.

Define all edge types: `LINKED_TO` (with `confidence_score` property), `USES`, `HAS_EMAIL`, `SAME_AS` (with `signal_breakdown` JSONB property), `OWNS_DOMAIN`, `LINKED_PHONE`, `REUSES_CRED`.

`upsert_account_node(platform: str, url: str, username: str, metadata: dict)` — creates or updates an Account node.

`upsert_identity_link(link: IdentityLink)` — creates a `SAME_AS` edge between two Account nodes with `confidence_score` and `signal_breakdown` as properties.

`build_graph_from_case(case_id: UUID, session)` — loads all evidence units and identity links for the case and builds the full graph in Neo4j.

`export_graph_for_plotly(case_id: UUID, max_nodes: int = 50) -> dict` — queries Neo4j for the top `max_nodes` nodes by confidence score and returns a dict with `nodes` (list of dicts with `id`, `label`, `platform`, `url`, `confidence`) and `edges` (list of dicts with `source`, `target`, `confidence`, `signals`). This dict is consumed directly by the Streamlit graph page.

---

## STEP 13 — MODULE 8: STREAMLIT DASHBOARD

### dashboard/app.py

Configure the Streamlit multi-page app. Set `page_title="SOCMINT — Suspect Profiling System"`, `layout="wide"`. Add a sidebar with: the SOCMINT logo (text-based), navigation links to all 5 pages, a system status widget that polls `GET /api/v1/health` every 30 seconds and shows green/red indicators for all 8 Docker services.

### dashboard/pages/1_case_intake.py

Implement Page 1: Case Intake. Show a form with input fields for every mandatory Legal Gate field from MODULE 1: `authority_id`, `agency_id`, `analyst_id`, `supervisor_approval` (checkbox), `purpose_statement` (text area, minimum 20 characters enforced with a character counter), `target_category` (selectbox with the 4 valid options), `jurisdiction` (text input), `retention_period` (number input, minimum 1), `seed_type` (radio: username / email / phone), `seed_value` (text input). On submit, call `POST /api/v1/cases/create`. Display validation errors inline if the API returns 422. On success, display the `case_id` and `run_id` and a "Pipeline started" confirmation. Store `case_id` in `st.session_state` for use by other pages.

### dashboard/pages/2_pipeline_status.py

Implement Page 2: Live Pipeline Status. Poll `GET /api/v1/pipeline/status/{case_id}` every 5 seconds using `st.empty()` and `time.sleep()`. Display 4 tier panels side by side (Tier 1 / Tier 2 / Tier 3 / Tier 4). Each panel shows: tier name and description, a per-tool status table with columns `Tool Name`, `Status` (running / done / failed / skipped), `Hits Found`. Show a live counter: total hits found so far, hits with preservation complete, HIGH confidence links detected. Show a "Platform Enrichment Queue" section that lists pending Tier 4 tool runs.

### dashboard/pages/3_identity_graph.py

Implement Page 3: Identity Graph. Call `GET /api/v1/graph/{case_id}` to get the Plotly-ready dict from `GraphBuilder.export_graph_for_plotly()`. Render using `plotly.graph_objects.Figure` with `go.Scatter` traces for nodes and edges. Colour nodes by platform using a consistent colour map. Colour edges by confidence tier: HIGH = red, MEDIUM = orange, LOW = yellow. Domain nodes displayed as squares (via `marker.symbol="square"`). Clicking a node calls `GET /api/v1/evidence/{case_id}/{platform}/{username}` and shows a full evidence panel in an expander: all `EvidenceUnit` records, `snapshot_ref`, `snapshot_hash` displayed as monospace, `wayback_ref` as a hyperlink, `platform_enrichment` data formatted as JSON.

### dashboard/pages/4_review_queue.py

Implement Page 4: Analyst Review Queue. Call `GET /api/v1/evidence/review-queue/{case_id}` to get HIGH confidence identity links awaiting decision. For each link, display a card with: Account A vs Account B, confidence score as a progress bar, signal breakdown table (signal name, weight, decay factor), screenshot thumbnail (loaded from MinIO via `GET /api/v1/evidence/{evidence_id}/screenshot`), Wayback archive hyperlink, platform enrichment summary, SHA-256 hash in monospace font. Below the card, show three buttons: `✓ CONFIRM`, `✗ REJECT`, `? UNCERTAIN`. A mandatory `Notes` text area must be filled before any button is enabled. On decision, call `POST /api/v1/evidence/review/{link_id}` with `{"decision": "CONFIRMED" | "REJECTED" | "FLAG_UNCERTAIN", "note": "..."}`. Write the decision to the audit log. After submission, remove the card from the queue and show a success toast.

### dashboard/pages/5_report.py

Implement Page 5: Report. Show the case summary: total evidence units, confirmed links, HIGH/MEDIUM/LOW counts. Show a "Generate Report" button. On click, call `POST /api/v1/reports/generate/{case_id}`. Poll for completion. On completion, show three download buttons: `Download JSON Evidence Package`, `Download PDF Summary Report`, `Download SHA-256 Bundle Hash`. The PDF must open cleanly and the JSON must be valid.

---

## STEP 14 — MODULE 9: REPORT GENERATOR

### api/services/report_generator.py

Implement `ReportGenerator` that assembles the full evidence package.

`generate_json_package(case_id: UUID, session) -> dict` — assembles a structured dict containing: case metadata, all confirmed identity links with full signal breakdowns, evidence chain per link (which tools at which tiers produced the signals), confidence scores with calibration notes, all preservation references (MinIO paths + SHA-256 hashes), all Wayback Machine archive references per hit, all platform enrichment data, analyst annotations and confirmation timestamps from the audit log, full audit log summary sorted by `created_at`.

`generate_pdf_report(case_id: UUID, json_package: dict) -> bytes` — generates a PDF using `reportlab`. The PDF must include: cover page with case ID, date, analyst ID, authority reference; executive summary section (number of seeds searched, platforms checked, accounts found, HIGH confidence links confirmed); identity link table per confirmed link; evidence chain per link; preservation evidence table with SHA-256 hashes and Wayback URLs; analyst review log; appendix with full tool execution audit.

`sign_bundle(json_bytes: bytes, pdf_bytes: bytes) -> str` — computes SHA-256 over the concatenation of the JSON and PDF bytes, returns the hex digest. This is stored as `{case_id}_bundle.sha256`.

`save_outputs(case_id: UUID, json_bytes: bytes, pdf_bytes: bytes, hash_str: str)` — stores all three output files in MinIO under `cases/{case_id}/reports/` and also writes them to a local `./cases/{case_id}/` directory for direct download via FastAPI.

---

## STEP 15 — FASTAPI ROUTERS

### api/routers/pipeline.py

`GET /api/v1/pipeline/status/{case_id}` — queries PostgreSQL for all tool execution audit log entries for the case. Returns a structured JSON with per-tier, per-tool status: `{"tier1": [{"tool": "blackbird", "status": "done", "hits": 5}, ...], "tier2": [...], "tier3": [...], "tier4": [...], "total_hits": N, "preservation_complete": N, "high_confidence_links": N}`.

### api/routers/evidence.py

`GET /api/v1/evidence/{case_id}` — returns all `EvidenceUnit` rows for the case as a list.

`GET /api/v1/evidence/review-queue/{case_id}` — returns all `IdentityLink` rows for the case where `confidence_tier = "HIGH"` and `analyst_decision IS NULL`.

`POST /api/v1/evidence/review/{link_id}` — accepts `{"decision": str, "note": str}`, updates the `identity_links` row, writes an audit log entry of type `"ANALYST_DECISION"`.

`GET /api/v1/evidence/{evidence_id}/screenshot` — streams the screenshot PNG from MinIO. Returns 404 if no screenshot exists.

### api/routers/reports.py

`POST /api/v1/reports/generate/{case_id}` — dispatches a Celery task to generate the report. Returns `{"task_id": ..., "status": "generating"}`.

`GET /api/v1/reports/status/{task_id}` — polls Celery task status. Returns `{"status": "PENDING" | "SUCCESS" | "FAILURE"}`.

`GET /api/v1/reports/download/{case_id}/json` — streams the JSON package from MinIO.

`GET /api/v1/reports/download/{case_id}/pdf` — streams the PDF from MinIO.

`GET /api/v1/reports/download/{case_id}/sha256` — returns the SHA-256 bundle hash as plain text.

### api/routers/graph.py

`GET /api/v1/graph/{case_id}` — calls `GraphBuilder.export_graph_for_plotly(case_id)` and returns the result as JSON.

`GET /api/v1/health` — checks all 8 services (PostgreSQL connection, Neo4j connection, Redis ping, MinIO bucket listing). Returns `{"postgres": "ok" | "error", "neo4j": "ok" | "error", "redis": "ok" | "error", "minio": "ok" | "error"}` with overall HTTP 200 if all are ok, HTTP 503 if any fail.

---

## STEP 16 — CELERY PIPELINE ORCHESTRATION

### worker_python/celery_app.py

Configure Celery with Redis as both broker and result backend. Set `task_serializer="json"`, `result_serializer="json"`. Register all task modules.

### Pipeline chord (triggered from `cases.py` on case creation)

The pipeline must execute in this order:
1. Fire `group(run_tier1_username_sweep.s(...), run_tier1_email_sweep.s(...))` immediately.
2. Fire `group(run_tier2_username_sweep.s(...), run_tier2_email_sweep.s(...))` immediately in parallel with Tier 1.
3. Fire `run_passive_recon.s(...)` as a background task (does not block the chord).
4. `chord([tier1_group, tier2_group], aggregate_results.s(case_id))` — after Tier 1 + Tier 2 complete, `aggregate_results` triggers the Correlation Engine.
5. Correlation Engine writes identity links to Neo4j.
6. For each HIGH or MEDIUM identity link, fire `run_platform_enrichment.s(platform, account_url, ...)` as a separate Celery task.

---

## STEP 17 — TESTS

### tests/test_legal_gate.py

Test all validation cases: missing mandatory field, `supervisor_approval=False`, short `purpose_statement`, invalid email format, invalid `seed_type`, valid input. Assert the correct error field names are returned.

### tests/test_evidence_schema.py

Test `EvidenceUnit` validation: valid unit created, invalid `result_type` raises `ValidationError`, invalid `tool_tier` raises `ValidationError`, `compute_hash` returns consistent SHA-256.

### tests/test_correlation_engine.py

Test the scoring formula: exact username match produces score 25, email match + username match produces score 45, 2-signal rule enforced (single signal below threshold not written), all conflict penalties applied correctly, evidence decay reduces old signals, Gravatar/Google/WhatsApp bonuses add correctly.

### tests/test_adapter_base.py

Test `ToolAdapter.execute()` with a mock subprocess: successful run returns `EvidenceUnit` list, failed health check returns `unavailable` unit, subprocess timeout returns `unavailable` unit, parse error returns `unavailable` unit with error in notes.

### tests/test_preservation.py

Test `PreservationService.preserve()` with mocked `httpx` and mocked MinIO: HTML fetched, SHA-256 computed correctly, MinIO upload called with correct path, Wayback Machine request sent, HTTP 429 handled gracefully.

### tests/test_report_generator.py

Test `generate_json_package()` returns all required keys, test `generate_pdf_report()` returns valid PDF bytes (check magic bytes `%PDF`), test `sign_bundle()` returns consistent SHA-256.

---

## STEP 18 — SCRIPTS

### scripts/compile_go_tools.sh

Shell script that uses `go install` to compile all Go binaries from their source repos (Enola, DetectDee, Mailsleuth, Email2WhatsApp, GoWitness, GitHound) and copies the compiled binaries to `worker_go/tools/go/`. Prints the Go version and confirms each binary exists after compilation.

### scripts/setup_ghunt.sh

Shell script that sets up GHunt authentication cookies. Prints instructions for the user to complete the OAuth flow and stores the cookies at the path defined by `GHUNT_COOKIES_PATH` in `.env`.

### scripts/seed_demo_case.py

Python script that calls `POST /api/v1/cases/create` with a pre-defined test case (using a publicly known, non-sensitive username that has multiple confirmed social accounts — use a well-known open-source developer or public figure who has documented multiple social accounts). Waits for the pipeline to complete and caches the results. Prints the `case_id` so the team can use it during the demo. This pre-populates PostgreSQL and Neo4j for the demo fallback.

### scripts/healthcheck.py

Python script that checks all 8 Docker containers are running, calls `GET /api/v1/health`, runs `adapter.health_check()` on each of the 35+ adapters, and prints a colour-coded table of which tools are installed and operational. Any failed health check is printed in red. Exit code 1 if any service or Tier 1 tool is unhealthy.

---

## CRITICAL CONSTRAINTS — READ BEFORE EVERY FILE YOU WRITE

**1. Canonical reference.** Every number, field name, weight, tool name, tier assignment, endpoint path, table name, column name, and Docker image tag is defined in `SOCMINT_PLAN_v2_0.txt`. If this prompt and the plan disagree, the plan wins.

**2. No codes.** The user explicitly does not want code snippets in this prompt. This is an instruction-only prompt. When GitHub Copilot reads this, it must generate the actual implementation files. Do not include any code blocks in this prompt document.

**3. Isolation over imports.** Every tool adapter must call the tool as a subprocess, not import it as a Python library. This is mandatory because Sherlock, Maigret, and GHunt have conflicting dependency trees. Each tool runs in its own installed environment within the container.

**4. Preservation before DB write.** The Preservation Service must run before any `EvidenceUnit` is written to PostgreSQL. This is defined explicitly in MODULE 4 of the plan.

**5. 2-signal rule is non-negotiable.** The Correlation Engine must enforce the minimum 2-signal corroboration rule. A single signal — regardless of weight — never produces an `identity_link` row in PostgreSQL or a `SAME_AS` edge in Neo4j.

**6. Audit log is append-only.** No `UPDATE` or `DELETE` may ever be called on the `audit_log` table. The database `GRANT INSERT` comment in the DDL reflects this. The application must only ever `INSERT` into this table.

**7. Legal gate is hard.** The pipeline absolutely must not start — no Celery tasks dispatched, no tool runs, no DB writes — until the Legal Gate validates all 10 mandatory fields. This is the primary ethical control.

**8. Tor for dorking.** `DorksEyeAdapter` and `DorksintAdapter` must route through Tor (`TOR_PROXY` env var). This is a hard requirement, not optional. `Email2WhatsAppAdapter` and `HuntPastebinAdapter` must also use Tor.

**9. Rate limits.** Every adapter that has a `cool_down_seconds` value in MODULE 3 of the plan must respect it using `time.sleep()` before each run.

**10. Demo fallback.** The `seed_demo_case.py` script must pre-populate the database so the demo can run from cache if any tool fails live. The `FallbackChainManager` must gracefully skip broken tools and continue the chain.

---

## FINAL CHECKLIST BEFORE SUBMITTING ANY FILE

Before writing each file, ask:
- Does this match `SOCMINT_PLAN_v2_0.txt` exactly?
- Is every field name, column name, and weight value taken from the plan?
- Does this adapter call the tool as a subprocess and parse the correct output format?
- Is `case_id` and `run_id` propagated to every `EvidenceUnit`?
- Is the audit log called for every significant event?
- Does the Legal Gate block the pipeline if called before validation passes?
- Is the Preservation Service called before the DB write?
- Is the 2-signal rule enforced before any Neo4j write?

---

*This prompt was generated from `SOCMINT_PLAN_v2_0.txt` (Hackathon Plan v2.0). The plan file is the canonical source of truth. This prompt is a structured expansion of it for GitHub Copilot. Build the implementation exactly as specified — do not add features, do not remove modules, do not change signal weights, do not change schema fields.*
