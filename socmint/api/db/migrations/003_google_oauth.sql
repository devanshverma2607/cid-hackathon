-- Migration 003: Add google_id column to users table for Google OAuth.
-- Run manually on existing databases; auto-applies on fresh volumes via
-- docker-entrypoint-initdb.d ordering.

ALTER TABLE users ADD COLUMN IF NOT EXISTS google_id TEXT UNIQUE;

-- Index for fast lookup by google_id during OAuth callback.
CREATE INDEX IF NOT EXISTS idx_users_google_id ON users (google_id) WHERE google_id IS NOT NULL;
