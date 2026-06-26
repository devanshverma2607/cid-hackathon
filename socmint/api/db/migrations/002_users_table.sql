-- =============================================================================
-- SOCMINT — Migration 002: Users table (Authentication & Registration)
--
-- Run against an existing database:
--   docker exec -i socmint-postgres-1 psql -U socmint -d socmint \
--       < api/db/migrations/002_users_table.sql
--
-- Safe to re-run: every statement is IF NOT EXISTS / IF EXISTS guarded.
-- =============================================================================

-- pgcrypto is already enabled by 01_schema.sql but guard it anyway.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS users (
    user_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username        TEXT NOT NULL UNIQUE,
    email           TEXT NOT NULL UNIQUE,
    hashed_password TEXT NOT NULL,
    full_name       TEXT NOT NULL DEFAULT '',
    role            TEXT NOT NULL DEFAULT 'analyst'
                    CHECK (role IN ('analyst', 'supervisor', 'admin')),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_email    ON users (email);
CREATE INDEX IF NOT EXISTS idx_users_username ON users (username);
