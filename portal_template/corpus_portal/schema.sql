CREATE SCHEMA IF NOT EXISTS corpus_portal;
CREATE TABLE IF NOT EXISTS corpus_portal.users (
    id UUID PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS corpus_portal.sessions (
    token_hash TEXT PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES corpus_portal.users(id),
    csrf_token TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS corpus_portal.projects (
    id UUID PRIMARY KEY,
    owner_id UUID NOT NULL REFERENCES corpus_portal.users(id),
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE corpus_portal.projects ADD COLUMN IF NOT EXISTS name_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS portal_project_owner_name_unique
ON corpus_portal.projects(owner_id,name_key) WHERE name_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS portal_project_owner ON corpus_portal.projects(owner_id);
CREATE TABLE IF NOT EXISTS corpus_portal.shares (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES corpus_portal.projects(id),
    token_hash TEXT NOT NULL UNIQUE,
    corpus_key TEXT NOT NULL,
    version TEXT NOT NULL,
    checksum_sha256 TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS corpus_portal.login_attempts (
    key_hash TEXT PRIMARY KEY,
    failures INT NOT NULL DEFAULT 0,
    window_start TIMESTAMPTZ NOT NULL DEFAULT now()
);
