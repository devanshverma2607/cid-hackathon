# SOCMINT — Suspect Profiling System

An OSINT pipeline that accepts a single seed (username, email, or phone number)
and automatically **discovers**, **correlates**, **preserves**, and **reports**
linked identities across 1000+ platforms using 35+ free, open-source OSINT tools.

The entire stack is free and open-source and runs locally via Docker Compose.
There are no paid APIs, no cloud accounts, and no licensing costs. The deliverable
is a working demo that runs end-to-end from a single seed input to a downloadable
PDF evidence report.

> Canonical specification: [`SOCMINT_PLAN_v2_0.txt`](SOCMINT_PLAN_v2_0.txt). Every
> architectural decision, module interface, scoring weight, schema field, and
> Docker container is defined there.

---

## Architecture

Eight Docker services orchestrate the pipeline:

| Service          | Image                      | Purpose                                   | Ports        |
| ---------------- | -------------------------- | ----------------------------------------- | ------------ |
| `postgres`       | `pgvector/pgvector:pg15`   | Relational store + pgvector similarity    | 5432         |
| `neo4j`          | `neo4j:5-community`        | Identity graph store                      | 7474, 7687   |
| `redis`          | `redis:7-alpine`           | Celery broker + result backend            | 6379         |
| `minio`          | `minio/minio`              | Object storage for preserved evidence     | 9000, 9001   |
| `worker_python`  | built locally              | Celery worker — Python tool adapters      | —            |
| `worker_go`      | built locally              | Celery worker — Go binary adapters        | —            |
| `api`            | built locally              | FastAPI gateway                           | 8000         |
| `dashboard`      | built locally              | Streamlit analyst UI                      | 8501         |

---

## Prerequisites

- **Docker** 24+ and **Docker Compose** v2
- **Go** 1.21+ (only required to compile the Go OSINT binaries)
- **Python** 3.11+ (only required to run helper scripts outside Docker)

---

## One-time setup

1. **Configure environment variables.**

   ```bash
   cp .env.example .env
   ```

   Open `.env` and fill in real values. At minimum set strong values for
   `POSTGRES_PASSWORD`, `NEO4J_PASSWORD`, and `MINIO_PASSWORD`. Optionally add
   `H8MAIL_API_KEY`, `HIBP_API_KEY`, and `INSTAGRAM_SESSION_ID` to enable the
   tools that need them.

2. **Compile the Go OSINT binaries.**

   ```bash
   bash scripts/compile_go_tools.sh
   ```

   This compiles Enola, DetectDee, Mailsleuth, Email2WhatsApp, GoWitness, and
   GitHound for `linux/amd64` and stores them in `worker_go/tools/go/`.

3. **Authenticate GHunt (Google account enrichment).**

   ```bash
   bash scripts/setup_ghunt.sh
   ```

   Follow the printed instructions to complete the OAuth flow. Cookies are stored
   at the path defined by `GHUNT_COOKIES_PATH` in `.env`.

---

## Starting the stack

```bash
docker compose up -d
```

Compose waits for `postgres`, `neo4j`, `redis`, and `minio` to report healthy
before starting the workers, API, and dashboard.

### Verify everything is healthy

```bash
python scripts/healthcheck.py
```

This checks all 8 containers, calls `GET /api/v1/health`, and runs
`health_check()` on every adapter, printing a colour-coded status table. It exits
non-zero if any service or Tier 1 tool is unhealthy.

---

## Accessing the services

| Service              | URL                              |
| -------------------- | -------------------------------- |
| Streamlit dashboard  | http://localhost:8501            |
| FastAPI docs         | http://localhost:8000/docs       |
| Neo4j Browser        | http://localhost:7474            |
| MinIO console        | http://localhost:9001            |

---

## Running the demo case

Pre-populate PostgreSQL and Neo4j with a cached run so the demo can fall back to
cached results if any tool fails live:

```bash
python scripts/seed_demo_case.py
```

The script prints the `case_id` to use during the demo.

---

## Running the tests

```bash
pytest tests/
```

The suite covers the legal gate, evidence schema, correlation engine, adapter
base class, preservation service, and report generator.

---

## Project layout

```
socmint/
├── docker-compose.yml      # 8-service stack definition
├── .env.example            # environment template
├── api/                    # FastAPI gateway, models, services, DB clients
├── worker_python/          # Celery worker + Python tool adapters
├── worker_go/              # Celery worker + Go binary adapters
├── dashboard/              # Streamlit multi-page analyst UI
├── tests/                  # pytest suite
└── scripts/                # setup, compile, seed, and healthcheck helpers
```

---

## Legal & ethical controls

This system enforces several hard controls that cannot be bypassed:

- **Legal Gate** — no pipeline starts until all 10 mandatory authorisation fields
  validate, including explicit supervisor approval.
- **Append-only audit log** — every tool run and analyst action is recorded; the
  `audit_log` table is insert-only.
- **2-signal corroboration rule** — no identity link is ever written from a single
  signal, regardless of its weight.
- **Evidence preservation** — every positive hit is screenshotted, hashed
  (SHA-256), and archived before it is written to the database.

For authorised investigative use only.
