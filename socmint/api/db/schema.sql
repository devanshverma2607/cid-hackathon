-- =============================================================================
-- SOCMINT — PostgreSQL DDL (Section 10.1 of SOCMINT_PLAN_v2_0.txt)
-- Tables: cases, evidence_units, identity_links, audit_log
-- =============================================================================

-- pgvector extension — required for semantic bio similarity (correlation engine)
CREATE EXTENSION IF NOT EXISTS vector;
-- gen_random_uuid() lives in pgcrypto on some builds; ensure it is available
CREATE EXTENSION IF NOT EXISTS pgcrypto;


-- -----------------------------------------------------------------------------
-- Cases table
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cases (
    case_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    authority_id        TEXT NOT NULL,
    agency_id           TEXT NOT NULL,
    analyst_id          TEXT NOT NULL,
    supervisor_approval BOOLEAN NOT NULL,
    purpose_statement   TEXT NOT NULL,
    target_category     TEXT NOT NULL,
    jurisdiction        TEXT NOT NULL,
    retention_period    INTEGER NOT NULL,
    seed_type           TEXT NOT NULL,
    seed_value          TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cases_analyst_id
    ON cases (analyst_id);


-- -----------------------------------------------------------------------------
-- Case seeds table — every subject identifier supplied at intake.
-- The primary seed is also denormalised onto cases(seed_type, seed_value) for
-- backward compatibility; this table holds the full "one or more identifiers"
-- set (usernames, emails, phones, profile URLs) and the resolved dispatch seed.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS case_seeds (
    seed_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id         UUID NOT NULL REFERENCES cases(case_id),
    seed_type       TEXT NOT NULL,          -- username | email | phone | profile_url
    seed_value      TEXT NOT NULL,          -- normalised identifier as supplied
    dispatch_type   TEXT NOT NULL,          -- the type the pipeline actually ran
    dispatch_value  TEXT NOT NULL,          -- the value the pipeline actually ran
    is_primary      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (case_id, seed_type, seed_value)
);

CREATE INDEX IF NOT EXISTS idx_case_seeds_case
    ON case_seeds (case_id);


-- -----------------------------------------------------------------------------
-- Evidence units table (v2.0 — extended)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evidence_units (
    evidence_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id              UUID NOT NULL REFERENCES cases(case_id),
    run_id               UUID NOT NULL,
    tool_name            TEXT NOT NULL,
    tool_version         TEXT NOT NULL,
    tool_tier            INTEGER NOT NULL,       -- 1=fast, 2=deep, 3=passive, 4=triggered
    source_platform      TEXT NOT NULL,
    source_tier          INTEGER NOT NULL,       -- 1=API, 2=public web, 3=archive, 4=inferred
    seed_type            TEXT NOT NULL,
    seed_value           TEXT NOT NULL,
    result_type          TEXT NOT NULL,
    result_value         TEXT NOT NULL,
    confidence_raw       FLOAT,
    signal_weights       JSONB,
    bio_embedding        vector(384),            -- pgvector semantic similarity
    image_embedding      vector(512),            -- pgvector CLIP avatar similarity
    face_embedding       vector(512),            -- pgvector FaceNet face similarity
    timestamp_collected  TIMESTAMPTZ NOT NULL,
    timestamp_preserved  TIMESTAMPTZ,
    snapshot_ref         TEXT,
    snapshot_hash        TEXT,
    wayback_ref          TEXT,
    platform_enrichment  JSONB,
    analyst_id           TEXT NOT NULL,
    notes                TEXT,
    UNIQUE (case_id, source_platform, result_value, seed_value)
);

CREATE INDEX IF NOT EXISTS idx_evidence_case_tier
    ON evidence_units (case_id, tool_tier);

CREATE INDEX IF NOT EXISTS idx_evidence_platform_value
    ON evidence_units (source_platform, result_value);

-- Deduplication key (mirrors the UNIQUE constraint, used by upsert/ON CONFLICT)
CREATE UNIQUE INDEX IF NOT EXISTS uq_evidence_dedup
    ON evidence_units (case_id, source_platform, result_value, seed_value);

-- Backfill for pre-existing databases (CREATE TABLE above only adds the column
-- on a fresh schema). CLIP ViT-B/32 avatar embeddings are 512-dimensional.
ALTER TABLE evidence_units
    ADD COLUMN IF NOT EXISTS image_embedding vector(512);

-- FaceNet (InceptionResnetV1/vggface2) face embeddings are 512-dimensional.
ALTER TABLE evidence_units
    ADD COLUMN IF NOT EXISTS face_embedding vector(512);


-- -----------------------------------------------------------------------------
-- Identity links table
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS identity_links (
    link_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id             UUID NOT NULL REFERENCES cases(case_id),
    account_a           TEXT NOT NULL,
    account_b           TEXT NOT NULL,
    platform_a          TEXT NOT NULL,
    platform_b          TEXT NOT NULL,
    confidence_score    FLOAT NOT NULL,
    confidence_tier     TEXT NOT NULL,
    signal_breakdown    JSONB NOT NULL,
    signal_count        INTEGER NOT NULL,
    analyst_decision    TEXT,
    analyst_note        TEXT,
    decided_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_identity_case_tier
    ON identity_links (case_id, confidence_tier);


-- -----------------------------------------------------------------------------
-- Audit log (append-only — no UPDATE, no DELETE at DB level)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    log_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id             UUID REFERENCES cases(case_id),
    run_id              UUID,
    event_type          TEXT NOT NULL,
    actor_id            TEXT NOT NULL,
    event_metadata      JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_case_event
    ON audit_log (case_id, event_type);

-- GRANT INSERT only — enforces append-only intent at the DB level.
-- The application must NEVER issue UPDATE or DELETE against audit_log.
-- Example (run by a DBA with a dedicated app role):
--   GRANT INSERT ON audit_log TO socmint_app;
