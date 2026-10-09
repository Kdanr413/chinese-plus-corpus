CREATE TABLE corpora (
    id UUID PRIMARY KEY,
    corpus_key VARCHAR(128) NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status VARCHAR(32) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'inactive', 'archived')),
    default_version_id UUID NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE corpus_versions (
    id UUID PRIMARY KEY,
    corpus_id UUID NOT NULL REFERENCES corpora(id) ON DELETE RESTRICT,
    version VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL
        CHECK (status IN ('draft', 'processing', 'ready', 'published', 'archived', 'failed', 'legacy')),
    searchable BOOLEAN NOT NULL DEFAULT FALSE,
    parent_version_id UUID NULL,
    release_path TEXT NOT NULL,
    corpus_format_version TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    search_engine_version TEXT NOT NULL,
    checksum_sha256 CHAR(64) NOT NULL
        CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (corpus_id, version),
    UNIQUE (corpus_id, id),
    CHECK (parent_version_id IS NULL OR parent_version_id <> id),
    CHECK (NOT searchable OR status IN ('ready', 'published')),
    FOREIGN KEY (corpus_id, parent_version_id)
        REFERENCES corpus_versions(corpus_id, id) ON DELETE RESTRICT
);

ALTER TABLE corpora
    ADD CONSTRAINT corpora_default_version_same_corpus_fk
    FOREIGN KEY (id, default_version_id)
    REFERENCES corpus_versions(corpus_id, id)
    ON DELETE RESTRICT
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE corpus_files (
    id UUID PRIMARY KEY,
    corpus_version_id UUID NOT NULL REFERENCES corpus_versions(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    role VARCHAR(32) NOT NULL
        CHECK (role IN ('corpus', 'index', 'metadata', 'statistics', 'legacy')),
    byte_size BIGINT NOT NULL CHECK (byte_size >= 0),
    checksum_sha256 CHAR(64) NOT NULL
        CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$'),
    media_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    encoding VARCHAR(32) NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (corpus_version_id, relative_path)
);

CREATE TABLE processing_jobs (
    id UUID PRIMARY KEY,
    corpus_id UUID NOT NULL REFERENCES corpora(id) ON DELETE RESTRICT,
    target_version_id UUID NULL,
    parent_version_id UUID NULL,
    workspace_path TEXT NOT NULL,
    job_type VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL
        CHECK (status IN ('created', 'queued', 'running', 'succeeded', 'failed', 'cancelled')),
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ NULL,
    completed_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (corpus_id, target_version_id)
        REFERENCES corpus_versions(corpus_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (corpus_id, parent_version_id)
        REFERENCES corpus_versions(corpus_id, id) ON DELETE RESTRICT
);

CREATE TABLE query_logs (
    id UUID PRIMARY KEY,
    corpus_id UUID NOT NULL REFERENCES corpora(id) ON DELETE RESTRICT,
    corpus_version_id UUID NOT NULL,
    query_text TEXT NOT NULL,
    command VARCHAR(32) NOT NULL,
    scope_ranges JSONB NOT NULL DEFAULT '[]'::jsonb,
    result_count BIGINT NULL CHECK (result_count IS NULL OR result_count >= 0),
    duration_ms INTEGER NULL CHECK (duration_ms IS NULL OR duration_ms >= 0),
    status VARCHAR(32) NOT NULL CHECK (status IN ('succeeded', 'failed')),
    error_code TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (corpus_id, corpus_version_id)
        REFERENCES corpus_versions(corpus_id, id) ON DELETE RESTRICT
);

CREATE INDEX corpus_versions_status_idx ON corpus_versions(status, searchable);
CREATE INDEX corpus_files_version_idx ON corpus_files(corpus_version_id);
CREATE INDEX processing_jobs_corpus_status_idx ON processing_jobs(corpus_id, status);
CREATE INDEX query_logs_version_created_idx ON query_logs(corpus_version_id, created_at DESC);

CREATE FUNCTION corpus_registry_set_updated_at() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$;

CREATE TRIGGER corpora_updated_at BEFORE UPDATE ON corpora
FOR EACH ROW EXECUTE FUNCTION corpus_registry_set_updated_at();
CREATE TRIGGER corpus_versions_updated_at BEFORE UPDATE ON corpus_versions
FOR EACH ROW EXECUTE FUNCTION corpus_registry_set_updated_at();
CREATE TRIGGER corpus_files_updated_at BEFORE UPDATE ON corpus_files
FOR EACH ROW EXECUTE FUNCTION corpus_registry_set_updated_at();
CREATE TRIGGER processing_jobs_updated_at BEFORE UPDATE ON processing_jobs
FOR EACH ROW EXECUTE FUNCTION corpus_registry_set_updated_at();
CREATE TRIGGER query_logs_updated_at BEFORE UPDATE ON query_logs
FOR EACH ROW EXECUTE FUNCTION corpus_registry_set_updated_at();
