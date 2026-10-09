-- Recognition/validation metadata only. Corpus text and model raw responses remain file assets.

CREATE TABLE recognition_runs (
    id UUID PRIMARY KEY,
    processing_job_id UUID NOT NULL UNIQUE
        REFERENCES processing_jobs(id) ON DELETE CASCADE,
    corpus_id UUID NOT NULL REFERENCES corpora(id) ON DELETE RESTRICT,
    status VARCHAR(32) NOT NULL DEFAULT 'created'
        CHECK (status IN ('created', 'running', 'succeeded', 'failed', 'cancelled')),
    workspace_path TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    config_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    prompt_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ NULL,
    completed_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE recognition_documents (
    id UUID PRIMARY KEY,
    recognition_run_id UUID NOT NULL REFERENCES recognition_runs(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL,
    source TEXT NOT NULL,
    raw_asset_path TEXT NOT NULL,
    clean_asset_path TEXT NOT NULL,
    raw_checksum_sha256 CHAR(64) NULL
        CHECK (raw_checksum_sha256 IS NULL OR raw_checksum_sha256 ~ '^[0-9a-f]{64}$'),
    clean_checksum_sha256 CHAR(64) NULL
        CHECK (clean_checksum_sha256 IS NULL OR clean_checksum_sha256 ~ '^[0-9a-f]{64}$'),
    processing_version TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (recognition_run_id, document_id),
    UNIQUE (recognition_run_id, id)
);

CREATE TABLE term_candidates (
    id UUID PRIMARY KEY,
    recognition_run_id UUID NOT NULL REFERENCES recognition_runs(id) ON DELETE CASCADE,
    recognition_document_id UUID NOT NULL,
    candidate_term_id TEXT NOT NULL,
    surface_form TEXT NOT NULL,
    normalized_form TEXT NOT NULL,
    start_offset INTEGER NULL CHECK (start_offset IS NULL OR start_offset >= 0),
    end_offset INTEGER NULL CHECK (end_offset IS NULL OR end_offset >= 0),
    sentence TEXT NOT NULL DEFAULT '',
    extractor TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    score DOUBLE PRECISION NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(32) NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'validated', 'reviewed', 'approved', 'released', 'rejected')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (recognition_run_id, candidate_term_id),
    FOREIGN KEY (recognition_run_id, recognition_document_id)
        REFERENCES recognition_documents(recognition_run_id, id) ON DELETE CASCADE,
    CHECK ((start_offset IS NULL AND end_offset IS NULL)
        OR (start_offset IS NOT NULL AND end_offset IS NOT NULL AND end_offset > start_offset))
);

CREATE TABLE topic_candidates (
    id UUID PRIMARY KEY,
    recognition_run_id UUID NOT NULL REFERENCES recognition_runs(id) ON DELETE CASCADE,
    recognition_document_id UUID NULL,
    candidate_topic_id TEXT NOT NULL,
    cluster_id TEXT NULL,
    topic_name TEXT NOT NULL,
    topic_definition TEXT NOT NULL DEFAULT '',
    keywords JSONB NOT NULL DEFAULT '[]'::jsonb,
    representative_texts JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_method TEXT NOT NULL,
    parent_candidate_id UUID NULL REFERENCES topic_candidates(id) ON DELETE RESTRICT,
    resolution VARCHAR(32) NOT NULL
        CHECK (resolution IN ('existing', 'unknown', 'new_topic')),
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(32) NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'validated', 'reviewed', 'approved', 'released', 'rejected')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (recognition_run_id, candidate_topic_id),
    FOREIGN KEY (recognition_run_id, recognition_document_id)
        REFERENCES recognition_documents(recognition_run_id, id) ON DELETE CASCADE,
    CHECK (recognition_document_id IS NOT NULL OR cluster_id IS NOT NULL)
);

CREATE TABLE validation_runs (
    id UUID PRIMARY KEY,
    recognition_run_id UUID NOT NULL REFERENCES recognition_runs(id) ON DELETE CASCADE,
    validation_kind VARCHAR(16) NOT NULL
        CHECK (validation_kind IN ('term', 'topic', 'cross')),
    aggregation_strategy VARCHAR(16) NOT NULL
        CHECK (aggregation_strategy IN ('majority', 'weighted', 'unanimous')),
    min_validators INTEGER NOT NULL CHECK (min_validators >= 1),
    validator_config JSONB NOT NULL DEFAULT '[]'::jsonb,
    status VARCHAR(32) NOT NULL DEFAULT 'created'
        CHECK (status IN ('created', 'running', 'succeeded', 'failed', 'cancelled', 'degraded')),
    error_message TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ NULL,
    completed_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE cross_validation_cases (
    id UUID PRIMARY KEY,
    recognition_run_id UUID NOT NULL REFERENCES recognition_runs(id) ON DELETE CASCADE,
    recognition_document_id UUID NOT NULL,
    topic_candidate_id UUID NULL REFERENCES topic_candidates(id) ON DELETE RESTRICT,
    mapped_topic_id TEXT NULL,
    context_asset_path TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (recognition_run_id, recognition_document_id)
        REFERENCES recognition_documents(recognition_run_id, id) ON DELETE CASCADE
);

CREATE TABLE cross_validation_terms (
    cross_validation_case_id UUID NOT NULL
        REFERENCES cross_validation_cases(id) ON DELETE CASCADE,
    term_candidate_id UUID NOT NULL REFERENCES term_candidates(id) ON DELETE RESTRICT,
    PRIMARY KEY (cross_validation_case_id, term_candidate_id)
);

CREATE TABLE validator_results (
    id UUID PRIMARY KEY,
    validation_run_id UUID NOT NULL REFERENCES validation_runs(id) ON DELETE CASCADE,
    term_candidate_id UUID NULL REFERENCES term_candidates(id) ON DELETE CASCADE,
    topic_candidate_id UUID NULL REFERENCES topic_candidates(id) ON DELETE CASCADE,
    cross_validation_case_id UUID NULL REFERENCES cross_validation_cases(id) ON DELETE CASCADE,
    validator TEXT NOT NULL,
    validator_version TEXT NOT NULL,
    model TEXT NOT NULL,
    model_version TEXT NOT NULL,
    prompt_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    decision VARCHAR(16) NOT NULL
        CHECK (decision IN ('accept', 'review', 'reject', 'uncertain')),
    structured_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    reason TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_response_asset_path TEXT NULL,
    raw_response_checksum_sha256 CHAR(64) NULL
        CHECK (raw_response_checksum_sha256 IS NULL OR raw_response_checksum_sha256 ~ '^[0-9a-f]{64}$'),
    status VARCHAR(16) NOT NULL DEFAULT 'succeeded'
        CHECK (status IN ('succeeded', 'failed', 'unavailable', 'degraded')),
    error_code TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NULL CHECK (duration_ms IS NULL OR duration_ms >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (num_nonnulls(term_candidate_id, topic_candidate_id, cross_validation_case_id) = 1)
);

CREATE TABLE validation_aggregates (
    id UUID PRIMARY KEY,
    validation_run_id UUID NOT NULL REFERENCES validation_runs(id) ON DELETE CASCADE,
    term_candidate_id UUID NULL REFERENCES term_candidates(id) ON DELETE CASCADE,
    topic_candidate_id UUID NULL REFERENCES topic_candidates(id) ON DELETE CASCADE,
    cross_validation_case_id UUID NULL REFERENCES cross_validation_cases(id) ON DELETE CASCADE,
    validator_count INTEGER NOT NULL CHECK (validator_count >= 0),
    accept_count INTEGER NOT NULL DEFAULT 0 CHECK (accept_count >= 0),
    reject_count INTEGER NOT NULL DEFAULT 0 CHECK (reject_count >= 0),
    review_count INTEGER NOT NULL DEFAULT 0 CHECK (review_count >= 0),
    uncertain_count INTEGER NOT NULL DEFAULT 0 CHECK (uncertain_count >= 0),
    agreement_score DOUBLE PRECISION NULL
        CHECK (agreement_score IS NULL OR (agreement_score >= 0 AND agreement_score <= 1)),
    aggregation_strategy VARCHAR(16) NOT NULL
        CHECK (aggregation_strategy IN ('majority', 'weighted', 'unanimous')),
    aggregated_decision VARCHAR(16) NOT NULL
        CHECK (aggregated_decision IN ('accept', 'review', 'reject', 'uncertain')),
    conflict BOOLEAN NOT NULL DEFAULT FALSE,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (num_nonnulls(term_candidate_id, topic_candidate_id, cross_validation_case_id) = 1),
    CHECK (accept_count + reject_count + review_count + uncertain_count <= validator_count)
);

CREATE TABLE resource_decisions (
    id UUID PRIMARY KEY,
    recognition_run_id UUID NOT NULL REFERENCES recognition_runs(id) ON DELETE CASCADE,
    recognition_document_id UUID NOT NULL,
    term_validation_status VARCHAR(16) NULL
        CHECK (term_validation_status IS NULL OR term_validation_status IN ('accept', 'review', 'reject', 'uncertain')),
    topic_validation_status VARCHAR(16) NULL
        CHECK (topic_validation_status IS NULL OR topic_validation_status IN ('accept', 'review', 'reject', 'uncertain')),
    cross_validation_status VARCHAR(16) NULL
        CHECK (cross_validation_status IS NULL OR cross_validation_status IN ('accept', 'review', 'reject', 'uncertain')),
    final_decision VARCHAR(24) NOT NULL
        CHECK (final_decision IN ('auto_accept', 'manual_review', 'reject', 'pending')),
    decision_rule TEXT NOT NULL,
    reason TEXT NOT NULL,
    lifecycle_status VARCHAR(16) NOT NULL DEFAULT 'candidate'
        CHECK (lifecycle_status IN ('candidate', 'validated', 'reviewed', 'approved', 'released', 'rejected')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (recognition_run_id, recognition_document_id),
    FOREIGN KEY (recognition_run_id, recognition_document_id)
        REFERENCES recognition_documents(recognition_run_id, id) ON DELETE CASCADE
);

CREATE TABLE human_reviews (
    id UUID PRIMARY KEY,
    resource_decision_id UUID NOT NULL REFERENCES resource_decisions(id) ON DELETE CASCADE,
    reviewer TEXT NOT NULL,
    action VARCHAR(32) NOT NULL
        CHECK (action IN ('confirm', 'modify_term', 'modify_topic', 'add_topic', 'merge_topic', 'reject')),
    before_state JSONB NOT NULL,
    after_state JSONB NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX recognition_runs_corpus_status_idx ON recognition_runs(corpus_id, status);
CREATE INDEX recognition_documents_run_idx ON recognition_documents(recognition_run_id);
CREATE INDEX term_candidates_document_idx ON term_candidates(recognition_document_id, status);
CREATE INDEX topic_candidates_run_status_idx ON topic_candidates(recognition_run_id, status);
CREATE INDEX validation_runs_recognition_kind_idx ON validation_runs(recognition_run_id, validation_kind);
CREATE INDEX validator_results_run_decision_idx ON validator_results(validation_run_id, decision);
CREATE INDEX validation_aggregates_run_decision_idx ON validation_aggregates(validation_run_id, aggregated_decision);
CREATE INDEX resource_decisions_run_final_idx ON resource_decisions(recognition_run_id, final_decision);
CREATE INDEX human_reviews_decision_created_idx ON human_reviews(resource_decision_id, created_at);

CREATE UNIQUE INDEX validator_results_term_once_idx
    ON validator_results(validation_run_id, validator, term_candidate_id)
    WHERE term_candidate_id IS NOT NULL;
CREATE UNIQUE INDEX validator_results_topic_once_idx
    ON validator_results(validation_run_id, validator, topic_candidate_id)
    WHERE topic_candidate_id IS NOT NULL;
CREATE UNIQUE INDEX validator_results_cross_once_idx
    ON validator_results(validation_run_id, validator, cross_validation_case_id)
    WHERE cross_validation_case_id IS NOT NULL;
CREATE UNIQUE INDEX validation_aggregates_term_once_idx
    ON validation_aggregates(validation_run_id, term_candidate_id)
    WHERE term_candidate_id IS NOT NULL;
CREATE UNIQUE INDEX validation_aggregates_topic_once_idx
    ON validation_aggregates(validation_run_id, topic_candidate_id)
    WHERE topic_candidate_id IS NOT NULL;
CREATE UNIQUE INDEX validation_aggregates_cross_once_idx
    ON validation_aggregates(validation_run_id, cross_validation_case_id)
    WHERE cross_validation_case_id IS NOT NULL;

CREATE TRIGGER recognition_runs_updated_at BEFORE UPDATE ON recognition_runs
FOR EACH ROW EXECUTE FUNCTION corpus_registry_set_updated_at();
CREATE TRIGGER recognition_documents_updated_at BEFORE UPDATE ON recognition_documents
FOR EACH ROW EXECUTE FUNCTION corpus_registry_set_updated_at();
CREATE TRIGGER term_candidates_updated_at BEFORE UPDATE ON term_candidates
FOR EACH ROW EXECUTE FUNCTION corpus_registry_set_updated_at();
CREATE TRIGGER topic_candidates_updated_at BEFORE UPDATE ON topic_candidates
FOR EACH ROW EXECUTE FUNCTION corpus_registry_set_updated_at();
CREATE TRIGGER validation_runs_updated_at BEFORE UPDATE ON validation_runs
FOR EACH ROW EXECUTE FUNCTION corpus_registry_set_updated_at();
CREATE TRIGGER cross_validation_cases_updated_at BEFORE UPDATE ON cross_validation_cases
FOR EACH ROW EXECUTE FUNCTION corpus_registry_set_updated_at();
CREATE TRIGGER resource_decisions_updated_at BEFORE UPDATE ON resource_decisions
FOR EACH ROW EXECUTE FUNCTION corpus_registry_set_updated_at();
