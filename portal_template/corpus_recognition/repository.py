"""PostgreSQL persistence for first-class recognition evidence.

The repository reuses the Stage 5 connection database boundary. It stores metadata,
decisions and asset references; corpus text and raw model responses stay in the
Workspace/Release filesystem.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from corpus_registry.config import DatabaseSettings
from corpus_registry.db import connect

from .errors import RecognitionNotFoundError


def _without_database_nul(value: Any) -> Any:
    """Recursively remove U+0000 before PostgreSQL text/jsonb persistence."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, Mapping):
        return {
            str(key).replace("\x00", ""): _without_database_nul(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_without_database_nul(item) for item in value)
    if isinstance(value, list):
        return [_without_database_nul(item) for item in value]
    if isinstance(value, set):
        return [_without_database_nul(item) for item in value]
    return value


def _json(value: Any) -> str:
    return json.dumps(_without_database_nul(value), ensure_ascii=False, default=str)


def _text(value: Any) -> str:
    """Return a PostgreSQL-safe text value without changing visible content."""
    return str(value).replace("\x00", "")


def _optional_text(value: Any | None) -> str | None:
    return None if value is None else _text(value)


def _relative_asset_path(value: str | Path) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("recognition asset paths must be relative to CORPUS_DATA_ROOT")
    return _text(path.as_posix())


def _require(row: dict[str, Any] | None, label: str) -> dict[str, Any]:
    if not row:
        raise RecognitionNotFoundError(f"{label} not found")
    return row


class RecognitionRepository:
    """CRUD boundary for migration 0002 recognition records."""

    def __init__(self, settings: DatabaseSettings | None = None) -> None:
        self.settings = settings or DatabaseSettings.from_environment()

    def create_run(
        self,
        processing_job_id: uuid.UUID,
        *,
        pipeline_version: str,
        config_snapshot: Mapping[str, Any],
        prompt_snapshot: Mapping[str, Any],
    ) -> dict[str, Any]:
        run_id = uuid.uuid4()
        with connect(self.settings) as connection, connection.transaction():
            job = connection.execute(
                "SELECT corpus_id, workspace_path FROM processing_jobs WHERE id=%s",
                (processing_job_id,),
            ).fetchone()
            _require(job, f"processing job {processing_job_id}")
            return connection.execute(
                """INSERT INTO recognition_runs(
                       id, processing_job_id, corpus_id, workspace_path,
                       pipeline_version, config_snapshot, prompt_snapshot)
                   VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb) RETURNING *""",
                (
                    run_id, processing_job_id, job["corpus_id"], job["workspace_path"],
                    _text(pipeline_version), _json(config_snapshot), _json(prompt_snapshot),
                ),
            ).fetchone()

    def get_run(self, run_id: uuid.UUID) -> dict[str, Any]:
        with connect(self.settings) as connection:
            row = connection.execute(
                "SELECT * FROM recognition_runs WHERE id=%s", (run_id,)
            ).fetchone()
        return _require(row, f"recognition run {run_id}")

    def update_run_status(
        self, run_id: uuid.UUID, status: str, error_message: str = ""
    ) -> dict[str, Any]:
        with connect(self.settings) as connection, connection.transaction():
            row = connection.execute(
                """UPDATE recognition_runs SET status=%s, error_message=%s,
                       started_at=CASE WHEN %s='running' THEN COALESCE(started_at, CURRENT_TIMESTAMP) ELSE started_at END,
                       completed_at=CASE WHEN %s IN ('succeeded','failed','cancelled') THEN CURRENT_TIMESTAMP ELSE completed_at END
                   WHERE id=%s RETURNING *""",
                (_text(status), _text(error_message), _text(status), _text(status), run_id),
            ).fetchone()
        return _require(row, f"recognition run {run_id}")

    def add_document(
        self,
        run_id: uuid.UUID,
        *,
        document_id: str,
        source: str,
        raw_asset_path: str | Path,
        clean_asset_path: str | Path,
        processing_version: str,
        metadata: Mapping[str, Any] | None = None,
        raw_checksum_sha256: str | None = None,
        clean_checksum_sha256: str | None = None,
    ) -> dict[str, Any]:
        with connect(self.settings) as connection, connection.transaction():
            return connection.execute(
                """INSERT INTO recognition_documents(
                       id, recognition_run_id, document_id, source, raw_asset_path,
                       clean_asset_path, raw_checksum_sha256, clean_checksum_sha256,
                       processing_version, metadata)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING *""",
                (
                    uuid.uuid4(), run_id, _text(document_id), _text(source),
                    _relative_asset_path(raw_asset_path), _relative_asset_path(clean_asset_path),
                    _optional_text(raw_checksum_sha256), _optional_text(clean_checksum_sha256),
                    _text(processing_version),
                    _json(metadata or {}),
                ),
            ).fetchone()

    def add_term_candidate(
        self,
        run_id: uuid.UUID,
        document_row_id: uuid.UUID,
        *,
        candidate_term_id: str,
        surface_form: str,
        normalized_form: str,
        extractor: str,
        extractor_version: str,
        start_offset: int | None = None,
        end_offset: int | None = None,
        sentence: str = "",
        score: float | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with connect(self.settings) as connection, connection.transaction():
            return connection.execute(
                """INSERT INTO term_candidates(
                       id, recognition_run_id, recognition_document_id, candidate_term_id,
                       surface_form, normalized_form, start_offset, end_offset, sentence,
                       extractor, extractor_version, score, evidence)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING *""",
                (
                    uuid.uuid4(), run_id, document_row_id, _text(candidate_term_id),
                    _text(surface_form), _text(normalized_form), start_offset, end_offset,
                    _text(sentence), _text(extractor), _text(extractor_version), score,
                    _json(evidence or {}),
                ),
            ).fetchone()

    def add_term_candidates_batch(
        self,
        run_id: uuid.UUID,
        candidates: Sequence[Mapping[str, Any]],
    ) -> dict[str, uuid.UUID]:
        """Insert term occurrences in one transaction and retain lightweight ids only."""
        if not candidates:
            return {}
        rows: list[tuple[Any, ...]] = []
        ids: dict[str, uuid.UUID] = {}
        for candidate in candidates:
            row_id = uuid.uuid4()
            candidate_term_id = _text(candidate["candidate_term_id"])
            ids[candidate_term_id] = row_id
            rows.append((
                row_id, run_id, candidate["document_row_id"], candidate_term_id,
                _text(candidate["surface_form"]), _text(candidate["normalized_form"]),
                candidate.get("start_offset"), candidate.get("end_offset"),
                _text(candidate.get("sentence", "")), _text(candidate["extractor"]),
                _text(candidate["extractor_version"]), candidate.get("score"),
                _json(candidate.get("evidence") or {}),
            ))
        with connect(self.settings) as connection, connection.transaction():
            with connection.cursor() as cursor:
                cursor.executemany(
                    """INSERT INTO term_candidates(
                           id, recognition_run_id, recognition_document_id, candidate_term_id,
                           surface_form, normalized_form, start_offset, end_offset, sentence,
                           extractor, extractor_version, score, evidence)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                    rows,
                )
        return ids

    def list_term_candidates(self, run_id: uuid.UUID) -> list[dict[str, Any]]:
        with connect(self.settings) as connection:
            return list(connection.execute(
                "SELECT * FROM term_candidates WHERE recognition_run_id=%s ORDER BY created_at, candidate_term_id",
                (run_id,),
            ).fetchall())

    def add_topic_candidate(
        self,
        run_id: uuid.UUID,
        *,
        candidate_topic_id: str,
        topic_name: str,
        source_method: str,
        resolution: str,
        document_row_id: uuid.UUID | None = None,
        cluster_id: str | None = None,
        topic_definition: str = "",
        keywords: Sequence[str] = (),
        representative_texts: Sequence[str] = (),
        parent_candidate_id: uuid.UUID | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with connect(self.settings) as connection, connection.transaction():
            return connection.execute(
                """INSERT INTO topic_candidates(
                       id, recognition_run_id, recognition_document_id, candidate_topic_id,
                       cluster_id, topic_name, topic_definition, keywords,
                       representative_texts, source_method, parent_candidate_id,
                       resolution, evidence)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s::jsonb)
                   RETURNING *""",
                (
                    uuid.uuid4(), run_id, document_row_id, _text(candidate_topic_id),
                    _optional_text(cluster_id), _text(topic_name), _text(topic_definition),
                    _json(list(keywords)), _json(list(representative_texts)),
                    _text(source_method), parent_candidate_id, _text(resolution),
                    _json(evidence or {}),
                ),
            ).fetchone()

    def add_topic_candidates_batch(
        self,
        run_id: uuid.UUID,
        candidates: Sequence[Mapping[str, Any]],
    ) -> dict[str, uuid.UUID]:
        """Insert topic candidates in one transaction and return their row ids."""
        if not candidates:
            return {}
        rows: list[tuple[Any, ...]] = []
        ids: dict[str, uuid.UUID] = {}
        for candidate in candidates:
            row_id = uuid.uuid4()
            candidate_topic_id = _text(candidate["candidate_topic_id"])
            ids[candidate_topic_id] = row_id
            rows.append((
                row_id, run_id, candidate.get("document_row_id"), candidate_topic_id,
                _optional_text(candidate.get("cluster_id")), _text(candidate["topic_name"]),
                _text(candidate.get("topic_definition", "")),
                _json(list(candidate.get("keywords") or ())),
                _json(list(candidate.get("representative_texts") or ())),
                _text(candidate["source_method"]), candidate.get("parent_candidate_id"),
                _text(candidate["resolution"]), _json(candidate.get("evidence") or {}),
            ))
        with connect(self.settings) as connection, connection.transaction():
            with connection.cursor() as cursor:
                cursor.executemany(
                    """INSERT INTO topic_candidates(
                           id, recognition_run_id, recognition_document_id, candidate_topic_id,
                           cluster_id, topic_name, topic_definition, keywords,
                           representative_texts, source_method, parent_candidate_id,
                           resolution, evidence)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s::jsonb)""",
                    rows,
                )
        return ids

    def list_topic_candidates(self, run_id: uuid.UUID) -> list[dict[str, Any]]:
        with connect(self.settings) as connection:
            return list(connection.execute(
                "SELECT * FROM topic_candidates WHERE recognition_run_id=%s ORDER BY created_at, candidate_topic_id",
                (run_id,),
            ).fetchall())

    def create_validation_run(
        self,
        run_id: uuid.UUID,
        *,
        validation_kind: str,
        aggregation_strategy: str,
        min_validators: int,
        validator_config: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        with connect(self.settings) as connection, connection.transaction():
            return connection.execute(
                """INSERT INTO validation_runs(
                       id, recognition_run_id, validation_kind, aggregation_strategy,
                       min_validators, validator_config)
                   VALUES (%s,%s,%s,%s,%s,%s::jsonb) RETURNING *""",
                (
                    uuid.uuid4(), run_id, _text(validation_kind), _text(aggregation_strategy),
                    min_validators, _json(list(validator_config)),
                ),
            ).fetchone()

    def update_validation_run_status(
        self, validation_run_id: uuid.UUID, status: str, error_message: str = ""
    ) -> dict[str, Any]:
        with connect(self.settings) as connection, connection.transaction():
            row = connection.execute(
                """UPDATE validation_runs SET status=%s, error_message=%s,
                       started_at=CASE WHEN %s='running' THEN COALESCE(started_at, CURRENT_TIMESTAMP) ELSE started_at END,
                       completed_at=CASE WHEN %s IN ('succeeded','failed','cancelled','degraded') THEN CURRENT_TIMESTAMP ELSE completed_at END
                   WHERE id=%s RETURNING *""",
                (_text(status), _text(error_message), _text(status), _text(status), validation_run_id),
            ).fetchone()
        return _require(row, f"validation run {validation_run_id}")

    def persist_term_validation(
        self,
        validation_run_id: uuid.UUID,
        term_candidate_id: uuid.UUID,
        *,
        results: Sequence[Mapping[str, Any]],
        aggregate: Mapping[str, Any],
        durations_ms: Sequence[int],
    ) -> dict[str, Any]:
        """Atomically store every independent opinion, its aggregate and run status."""
        if len(results) != len(durations_ms):
            raise ValueError("results and durations_ms must have the same length")
        result_ids: list[uuid.UUID] = []
        aggregate_id = uuid.uuid4()
        degraded = any(str(row.get("status", "")) != "succeeded" for row in results)
        with connect(self.settings) as connection, connection.transaction():
            subject = connection.execute(
                """SELECT 1 FROM validation_runs v
                   JOIN term_candidates t ON t.recognition_run_id=v.recognition_run_id
                   WHERE v.id=%s AND v.validation_kind='term' AND t.id=%s""",
                (validation_run_id, term_candidate_id),
            ).fetchone()
            if not subject:
                raise RecognitionNotFoundError(
                    "term candidate and term validation run must belong to the same recognition run"
                )
            for row, duration in zip(results, durations_ms):
                result_id = uuid.uuid4()
                connection.execute(
                    """INSERT INTO validator_results(
                           id, validation_run_id, term_candidate_id, validator,
                           validator_version, model, model_version, prompt_id,
                           prompt_version, decision, structured_result, reason,
                           evidence, status, error_code, duration_ms)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,%s)""",
                    (
                        result_id, validation_run_id, term_candidate_id,
                        _text(row["validator"]), _text(row["validator_version"]),
                        _text(row["model"]), _text(row["model_version"]),
                        _text(row["prompt_id"]), _text(row["prompt_version"]),
                        _text(row["decision"]), _json(row.get("structured_result", {})),
                        _text(row["reason"]), _json(row.get("evidence", {})),
                        _text(row.get("status", "succeeded")),
                        _text(row.get("error_code", "")), duration,
                    ),
                )
                result_ids.append(result_id)
            details = dict(aggregate.get("details", {}))
            details["validator_result_ids"] = [str(value) for value in result_ids]
            connection.execute(
                """INSERT INTO validation_aggregates(
                       id, validation_run_id, term_candidate_id, validator_count,
                       accept_count, reject_count, review_count, uncertain_count,
                       agreement_score, aggregation_strategy, aggregated_decision,
                       conflict, details)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                (
                    aggregate_id, validation_run_id, term_candidate_id,
                    aggregate["validator_count"], aggregate["accept_count"],
                    aggregate["reject_count"], aggregate["review_count"],
                    aggregate["uncertain_count"], aggregate.get("agreement_score"),
                    _text(aggregate["aggregation_strategy"]),
                    _text(aggregate["aggregated_decision"]),
                    aggregate["conflict"], _json(details),
                ),
            )
            connection.execute(
                """UPDATE validation_runs SET status=%s, completed_at=CURRENT_TIMESTAMP
                   WHERE id=%s""",
                ("degraded" if degraded else "succeeded", validation_run_id),
            )
        return {
            "validator_result_ids": tuple(result_ids),
            "aggregate_id": aggregate_id,
            "status": "degraded" if degraded else "succeeded",
        }

    def get_validation_aggregate(
        self, validation_run_id: uuid.UUID, *, term_candidate_id: uuid.UUID
    ) -> dict[str, Any]:
        with connect(self.settings) as connection:
            row = connection.execute(
                """SELECT * FROM validation_aggregates
                   WHERE validation_run_id=%s AND term_candidate_id=%s""",
                (validation_run_id, term_candidate_id),
            ).fetchone()
        return _require(row, "term validation aggregate")

    def persist_validation(
        self,
        validation_run_id: uuid.UUID,
        subject_type: str,
        subject_id: uuid.UUID,
        *,
        results: Sequence[Mapping[str, Any]],
        aggregate: Mapping[str, Any],
        durations_ms: Sequence[int],
    ) -> dict[str, Any]:
        """Atomically persist topic/cross opinions, aggregate and run status."""
        columns = {"topic": "topic_candidate_id", "cross": "cross_validation_case_id"}
        if subject_type not in columns:
            raise ValueError("subject_type must be topic or cross")
        if len(results) != len(durations_ms):
            raise ValueError("results and durations_ms must have the same length")
        column = columns[subject_type]
        aggregate_id = uuid.uuid4()
        result_ids: list[uuid.UUID] = []
        degraded = any(str(row.get("status", "")) != "succeeded" for row in results)
        with connect(self.settings) as connection, connection.transaction():
            valid = connection.execute(
                f"""SELECT 1 FROM validation_runs v
                    JOIN {'topic_candidates t' if subject_type == 'topic' else 'cross_validation_cases t'}
                      ON t.recognition_run_id=v.recognition_run_id
                    WHERE v.id=%s AND v.validation_kind=%s AND t.id=%s""",
                (validation_run_id, subject_type, subject_id),
            ).fetchone()
            if not valid:
                raise RecognitionNotFoundError(
                    f"{subject_type} subject and validation run must belong to the same recognition run"
                )
            for row, duration in zip(results, durations_ms):
                result_id = uuid.uuid4()
                connection.execute(
                    f"""INSERT INTO validator_results(
                        id, validation_run_id, {column}, validator, validator_version,
                        model, model_version, prompt_id, prompt_version, decision,
                        structured_result, reason, evidence, status, error_code, duration_ms)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,%s)""",
                    (
                        result_id, validation_run_id, subject_id, _text(row["validator"]),
                        _text(row["validator_version"]), _text(row["model"]),
                        _text(row["model_version"]), _text(row["prompt_id"]),
                        _text(row["prompt_version"]), _text(row["decision"]),
                        _json(row.get("structured_result", {})), _text(row["reason"]),
                        _json(row.get("evidence", {})),
                        _text(row.get("status", "succeeded")),
                        _text(row.get("error_code", "")), duration,
                    ),
                )
                result_ids.append(result_id)
            details = dict(aggregate.get("details", {}))
            details["validator_result_ids"] = [str(value) for value in result_ids]
            connection.execute(
                f"""INSERT INTO validation_aggregates(
                    id, validation_run_id, {column}, validator_count, accept_count,
                    reject_count, review_count, uncertain_count, agreement_score,
                    aggregation_strategy, aggregated_decision, conflict, details)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                (
                    aggregate_id, validation_run_id, subject_id,
                    aggregate["validator_count"], aggregate["accept_count"],
                    aggregate["reject_count"], aggregate["review_count"],
                    aggregate["uncertain_count"], aggregate.get("agreement_score"),
                    _text(aggregate["aggregation_strategy"]),
                    _text(aggregate["aggregated_decision"]),
                    aggregate["conflict"], _json(details),
                ),
            )
            connection.execute(
                "UPDATE validation_runs SET status=%s, completed_at=CURRENT_TIMESTAMP WHERE id=%s",
                ("degraded" if degraded else "succeeded", validation_run_id),
            )
        return {"validator_result_ids": tuple(result_ids), "aggregate_id": aggregate_id}

    def list_validation_runs(self, run_id: uuid.UUID) -> list[dict[str, Any]]:
        with connect(self.settings) as connection:
            return list(connection.execute(
                "SELECT * FROM validation_runs WHERE recognition_run_id=%s ORDER BY created_at",
                (run_id,),
            ).fetchall())

    def list_validation_aggregates(self, run_id: uuid.UUID) -> list[dict[str, Any]]:
        with connect(self.settings) as connection:
            return list(connection.execute(
                """SELECT a.* FROM validation_aggregates a
                   JOIN validation_runs v ON v.id=a.validation_run_id
                   WHERE v.recognition_run_id=%s ORDER BY a.created_at""",
                (run_id,),
            ).fetchall())

    def add_cross_validation_case(
        self,
        run_id: uuid.UUID,
        document_row_id: uuid.UUID,
        *,
        term_candidate_ids: Sequence[uuid.UUID],
        topic_candidate_id: uuid.UUID | None = None,
        mapped_topic_id: str | None = None,
        context_asset_path: str | Path | None = None,
    ) -> dict[str, Any]:
        case_id = uuid.uuid4()
        stored_context = _relative_asset_path(context_asset_path) if context_asset_path else None
        with connect(self.settings) as connection, connection.transaction():
            row = connection.execute(
                """INSERT INTO cross_validation_cases(
                       id, recognition_run_id, recognition_document_id,
                             topic_candidate_id, mapped_topic_id, context_asset_path)
                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
                (
                    case_id, run_id, document_row_id, topic_candidate_id,
                    _optional_text(mapped_topic_id), stored_context,
                ),
            ).fetchone()
            for term_id in term_candidate_ids:
                connection.execute(
                    "INSERT INTO cross_validation_terms(cross_validation_case_id, term_candidate_id) VALUES (%s,%s)",
                    (case_id, term_id),
                )
        return row

    def record_validator_result(
        self,
        validation_run_id: uuid.UUID,
        *,
        subject_type: str,
        subject_id: uuid.UUID,
        validator: str,
        validator_version: str,
        model: str,
        model_version: str,
        prompt_id: str,
        prompt_version: str,
        decision: str,
        reason: str,
        structured_result: Mapping[str, Any],
        evidence: Mapping[str, Any],
        status: str = "succeeded",
        error_code: str = "",
        duration_ms: int | None = None,
        raw_response_asset_path: str | Path | None = None,
        raw_response_checksum_sha256: str | None = None,
    ) -> dict[str, Any]:
        columns = {
            "term": "term_candidate_id",
            "topic": "topic_candidate_id",
            "cross": "cross_validation_case_id",
        }
        try:
            subject_column = columns[subject_type]
        except KeyError as exc:
            raise ValueError("subject_type must be term, topic or cross") from exc
        raw_path = _relative_asset_path(raw_response_asset_path) if raw_response_asset_path else None
        sql = f"""INSERT INTO validator_results(
                     id, validation_run_id, {subject_column}, validator, validator_version,
                     model, model_version, prompt_id, prompt_version, decision,
                     structured_result, reason, evidence, raw_response_asset_path,
                     raw_response_checksum_sha256, status, error_code, duration_ms)
                 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,%s,%s,%s)
                 RETURNING *"""
        with connect(self.settings) as connection, connection.transaction():
            return connection.execute(
                sql,
                (
                    uuid.uuid4(), validation_run_id, subject_id, _text(validator),
                    _text(validator_version), _text(model), _text(model_version),
                    _text(prompt_id), _text(prompt_version), _text(decision),
                    _json(structured_result), _text(reason), _json(evidence), raw_path,
                    _optional_text(raw_response_checksum_sha256), _text(status),
                    _text(error_code), duration_ms,
                ),
            ).fetchone()

    def list_validator_results(self, validation_run_id: uuid.UUID) -> list[dict[str, Any]]:
        with connect(self.settings) as connection:
            return list(connection.execute(
                "SELECT * FROM validator_results WHERE validation_run_id=%s ORDER BY created_at, validator",
                (validation_run_id,),
            ).fetchall())

    def record_aggregate(
        self,
        validation_run_id: uuid.UUID,
        *,
        subject_type: str,
        subject_id: uuid.UUID,
        validator_count: int,
        accept_count: int,
        reject_count: int,
        review_count: int,
        uncertain_count: int,
        agreement_score: float | None,
        aggregation_strategy: str,
        aggregated_decision: str,
        conflict: bool,
        details: Mapping[str, Any],
    ) -> dict[str, Any]:
        columns = {"term": "term_candidate_id", "topic": "topic_candidate_id", "cross": "cross_validation_case_id"}
        if subject_type not in columns:
            raise ValueError("subject_type must be term, topic or cross")
        sql = f"""INSERT INTO validation_aggregates(
                     id, validation_run_id, {columns[subject_type]}, validator_count,
                     accept_count, reject_count, review_count, uncertain_count,
                     agreement_score, aggregation_strategy, aggregated_decision, conflict, details)
                 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING *"""
        with connect(self.settings) as connection, connection.transaction():
            return connection.execute(
                sql,
                (
                    uuid.uuid4(), validation_run_id, subject_id, validator_count,
                    accept_count, reject_count, review_count, uncertain_count,
                    agreement_score, _text(aggregation_strategy),
                    _text(aggregated_decision), conflict,
                    _json(details),
                ),
            ).fetchone()

    def create_resource_decision(
        self,
        run_id: uuid.UUID,
        document_row_id: uuid.UUID,
        *,
        term_validation_status: str | None,
        topic_validation_status: str | None,
        cross_validation_status: str | None,
        final_decision: str,
        decision_rule: str,
        reason: str,
        lifecycle_status: str = "candidate",
    ) -> dict[str, Any]:
        with connect(self.settings) as connection, connection.transaction():
            return connection.execute(
                """INSERT INTO resource_decisions(
                       id, recognition_run_id, recognition_document_id,
                       term_validation_status, topic_validation_status,
                       cross_validation_status, final_decision, decision_rule,
                       reason, lifecycle_status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (
                    uuid.uuid4(), run_id, document_row_id,
                    _optional_text(term_validation_status),
                    _optional_text(topic_validation_status),
                    _optional_text(cross_validation_status), _text(final_decision),
                    _text(decision_rule), _text(reason), _text(lifecycle_status),
                ),
            ).fetchone()

    def add_resource_decisions_batch(
        self, run_id: uuid.UUID, decisions: Sequence[Mapping[str, Any]]
    ) -> int:
        """Insert final per-document decisions in one transaction."""
        if not decisions:
            return 0
        rows = [(
            row["id"], run_id, row["document_row_id"],
            _optional_text(row.get("term_validation_status")),
            _optional_text(row.get("topic_validation_status")),
            _optional_text(row.get("cross_validation_status")),
            _text(row["final_decision"]), _text(row["decision_rule"]),
            _text(row["reason"]), _text(row.get("lifecycle_status", "candidate")),
        ) for row in decisions]
        with connect(self.settings) as connection, connection.transaction():
            with connection.cursor() as cursor:
                cursor.executemany(
                    """INSERT INTO resource_decisions(
                           id, recognition_run_id, recognition_document_id,
                           term_validation_status, topic_validation_status,
                           cross_validation_status, final_decision, decision_rule,
                           reason, lifecycle_status)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    rows,
                )
        return len(rows)

    def list_resource_decisions(self, run_id: uuid.UUID) -> list[dict[str, Any]]:
        with connect(self.settings) as connection:
            return list(connection.execute(
                """SELECT d.*, rd.document_id FROM resource_decisions d
                   JOIN recognition_documents rd ON rd.id=d.recognition_document_id
                   WHERE d.recognition_run_id=%s ORDER BY rd.document_id""",
                (run_id,),
            ).fetchall())

    def update_resource_decision_result(
        self,
        decision_id: uuid.UUID,
        *,
        term_validation_status: str | None,
        topic_validation_status: str | None,
        cross_validation_status: str | None,
        final_decision: str,
        decision_rule: str,
        reason: str,
        lifecycle_status: str,
    ) -> dict[str, Any]:
        """Finalize a decision row that was created before the human annotation gate."""
        with connect(self.settings) as connection, connection.transaction():
            return _require(connection.execute(
                """UPDATE resource_decisions
                   SET term_validation_status=%s, topic_validation_status=%s,
                       cross_validation_status=%s, final_decision=%s,
                       decision_rule=%s, reason=%s, lifecycle_status=%s,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=%s RETURNING *""",
                (
                    _optional_text(term_validation_status),
                    _optional_text(topic_validation_status),
                    _optional_text(cross_validation_status), _text(final_decision),
                    _text(decision_rule), _text(reason), _text(lifecycle_status), decision_id,
                ),
            ).fetchone(), f"resource decision {decision_id}")

    def get_run_bundle(self, run_id: uuid.UUID) -> dict[str, Any]:
        run = self.get_run(run_id)
        with connect(self.settings) as connection:
            documents = list(connection.execute(
                "SELECT * FROM recognition_documents WHERE recognition_run_id=%s ORDER BY document_id",
                (run_id,),
            ).fetchall())
            terms = list(connection.execute(
                "SELECT * FROM term_candidates WHERE recognition_run_id=%s ORDER BY candidate_term_id",
                (run_id,),
            ).fetchall())
            topics = list(connection.execute(
                "SELECT * FROM topic_candidates WHERE recognition_run_id=%s ORDER BY candidate_topic_id",
                (run_id,),
            ).fetchall())
            validation_runs = list(connection.execute(
                "SELECT * FROM validation_runs WHERE recognition_run_id=%s ORDER BY created_at",
                (run_id,),
            ).fetchall())
            validator_results = list(connection.execute(
                """SELECT vr.* FROM validator_results vr
                   JOIN validation_runs v ON v.id=vr.validation_run_id
                   WHERE v.recognition_run_id=%s ORDER BY vr.created_at""",
                (run_id,),
            ).fetchall())
            aggregates = list(connection.execute(
                """SELECT a.* FROM validation_aggregates a
                   JOIN validation_runs v ON v.id=a.validation_run_id
                   WHERE v.recognition_run_id=%s ORDER BY a.created_at""",
                (run_id,),
            ).fetchall())
            decisions = self.list_resource_decisions(run_id)
            reviews = list(connection.execute(
                """SELECT h.* FROM human_reviews h
                   JOIN resource_decisions d ON d.id=h.resource_decision_id
                   WHERE d.recognition_run_id=%s ORDER BY h.created_at""",
                (run_id,),
            ).fetchall())
        return {
            "run": run, "documents": documents, "term_candidates": terms,
            "topic_candidates": topics, "validation_runs": validation_runs,
            "validator_results": validator_results, "validation_aggregates": aggregates,
            "resource_decisions": decisions, "human_reviews": reviews,
        }

    def get_run_counts(self, run_id: uuid.UUID) -> dict[str, int]:
        """Return lightweight evidence counts without materializing a full run bundle."""
        self.get_run(run_id)
        queries = {
            "documents": "SELECT count(*) FROM recognition_documents WHERE recognition_run_id=%s",
            "term_candidates": "SELECT count(*) FROM term_candidates WHERE recognition_run_id=%s",
            "topic_candidates": "SELECT count(*) FROM topic_candidates WHERE recognition_run_id=%s",
            "validation_runs": "SELECT count(*) FROM validation_runs WHERE recognition_run_id=%s",
            "resource_decisions": "SELECT count(*) FROM resource_decisions WHERE recognition_run_id=%s",
        }
        with connect(self.settings) as connection:
            return {
                name: int(connection.execute(sql, (run_id,)).fetchone()["count"])
                for name, sql in queries.items()
            }

    def get_approved_resource_bundle(self, run_id: uuid.UUID) -> dict[str, Any]:
        """Load only the columns needed to build final approved artifacts."""
        run = self.get_run(run_id)
        with connect(self.settings) as connection:
            documents = list(connection.execute(
                """SELECT id, document_id, source, clean_asset_path, metadata
                   FROM recognition_documents
                   WHERE recognition_run_id=%s ORDER BY document_id""",
                (run_id,),
            ).fetchall())
            terms = list(connection.execute(
                """SELECT recognition_document_id, candidate_term_id, normalized_form, score
                   FROM term_candidates
                   WHERE recognition_run_id=%s AND status='approved'
                   ORDER BY recognition_document_id, candidate_term_id""",
                (run_id,),
            ).fetchall())
            topics = list(connection.execute(
                """SELECT recognition_document_id, candidate_topic_id, topic_name, evidence
                   FROM topic_candidates
                   WHERE recognition_run_id=%s AND status='approved'
                   ORDER BY recognition_document_id, candidate_topic_id""",
                (run_id,),
            ).fetchall())
            decisions = list(connection.execute(
                """SELECT * FROM resource_decisions
                   WHERE recognition_run_id=%s AND lifecycle_status='approved'
                   ORDER BY recognition_document_id""",
                (run_id,),
            ).fetchall())
            reviews = list(connection.execute(
                """SELECT h.* FROM human_reviews h
                   JOIN resource_decisions d ON d.id=h.resource_decision_id
                   WHERE d.recognition_run_id=%s ORDER BY h.created_at""",
                (run_id,),
            ).fetchall())
        return {
            "run": run,
            "documents": documents,
            "term_candidates": terms,
            "topic_candidates": topics,
            "resource_decisions": decisions,
            "human_reviews": reviews,
        }

    def update_candidate_status(self, kind: str, candidate_id: uuid.UUID, status: str) -> dict[str, Any]:
        tables = {"term": "term_candidates", "topic": "topic_candidates"}
        if kind not in tables:
            raise ValueError("kind must be term or topic")
        with connect(self.settings) as connection, connection.transaction():
            row = connection.execute(
                f"UPDATE {tables[kind]} SET status=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s RETURNING *",
                (_text(status), candidate_id),
            ).fetchone()
        return _require(row, f"{kind} candidate {candidate_id}")

    def update_term_candidate_status_for_run(
        self, run_id: uuid.UUID, status: str
    ) -> int:
        """Update every term occurrence in a run without one connection per row."""
        with connect(self.settings) as connection, connection.transaction():
            cursor = connection.execute(
                """UPDATE term_candidates SET status=%s, updated_at=CURRENT_TIMESTAMP
                   WHERE recognition_run_id=%s""",
                (_text(status), run_id),
            )
            return cursor.rowcount

    def approve_candidates_for_approved_decisions(self, run_id: uuid.UUID) -> dict[str, int]:
        """Promote candidate rows whose document has an approved final decision."""
        counts: dict[str, int] = {}
        with connect(self.settings) as connection, connection.transaction():
            term_cursor = connection.execute(
                """UPDATE term_candidates AS candidate
                   SET status='approved', updated_at=CURRENT_TIMESTAMP
                   FROM resource_decisions AS decision
                   WHERE candidate.recognition_run_id=%s
                     AND decision.recognition_run_id=candidate.recognition_run_id
                     AND decision.recognition_document_id=candidate.recognition_document_id
                     AND decision.lifecycle_status='approved'""",
                (run_id,),
            )
            counts["term"] = term_cursor.rowcount
            topic_cursor = connection.execute(
                """UPDATE topic_candidates AS candidate
                   SET status='approved', updated_at=CURRENT_TIMESTAMP
                   FROM resource_decisions AS decision
                   WHERE candidate.recognition_run_id=%s
                     AND decision.recognition_run_id=candidate.recognition_run_id
                     AND decision.recognition_document_id=candidate.recognition_document_id
                     AND decision.lifecycle_status='approved'""",
                (run_id,),
            )
            counts["topic"] = topic_cursor.rowcount
        return counts

    def revise_topic_candidate(
        self,
        candidate_id: uuid.UUID,
        *,
        topic_name: str,
        evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Apply a cross-validation correction while retaining its evidence."""
        with connect(self.settings) as connection, connection.transaction():
            row = connection.execute(
                """UPDATE topic_candidates
                   SET topic_name=%s, evidence=%s, updated_at=CURRENT_TIMESTAMP
                   WHERE id=%s RETURNING *""",
                (_text(topic_name), _json(evidence), candidate_id),
            ).fetchone()
        return _require(row, f"topic candidate {candidate_id}")

    def apply_human_review(
        self,
        resource_decision_id: uuid.UUID,
        *,
        reviewer: str,
        action: str,
        after_state: Mapping[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        """Append provenance and update lifecycle without overwriting automatic evidence."""
        allowed = {"confirm", "modify_term", "modify_topic", "add_topic", "merge_topic", "reject"}
        if action not in allowed:
            raise ValueError(f"unsupported review action: {action}")
        if not reviewer.strip() or not reason.strip():
            raise ValueError("reviewer and reason are required")
        with connect(self.settings) as connection, connection.transaction():
            current = connection.execute(
                "SELECT * FROM resource_decisions WHERE id=%s FOR UPDATE",
                (resource_decision_id,),
            ).fetchone()
            current = _require(current, f"resource decision {resource_decision_id}")
            previous_review = connection.execute(
                """SELECT after_state FROM human_reviews
                   WHERE resource_decision_id=%s ORDER BY created_at DESC LIMIT 1""",
                (resource_decision_id,),
            ).fetchone()
            merged_after_state = dict((previous_review or {}).get("after_state") or {})
            merged_after_state.update(dict(after_state))
            review = connection.execute(
                """INSERT INTO human_reviews(
                       id, resource_decision_id, reviewer, action,
                       before_state, after_state, reason)
                   VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s) RETURNING *""",
                (
                    uuid.uuid4(), resource_decision_id, _text(reviewer.strip()),
                    _text(action), _json(current), _json(merged_after_state),
                    _text(reason.strip()),
                ),
            ).fetchone()
            # A submitted modification/add/merge is itself a reviewed human
            # decision.  Preserve the automatic value in before_state and the
            # edited value in after_state, then allow the confirmed result to
            # enter the approved output tables without a second click.
            lifecycle = "rejected" if action == "reject" else "approved"
            final_decision = "reject" if action == "reject" else current["final_decision"]
            updated = connection.execute(
                """UPDATE resource_decisions
                   SET lifecycle_status=%s, final_decision=%s, updated_at=CURRENT_TIMESTAMP
                   WHERE id=%s RETURNING *""",
                (lifecycle, final_decision, resource_decision_id),
            ).fetchone()
            candidate_status = "rejected" if action == "reject" else "approved"
            connection.execute(
                """UPDATE term_candidates SET status=%s, updated_at=CURRENT_TIMESTAMP
                   WHERE recognition_run_id=%s AND recognition_document_id=%s""",
                (candidate_status, current["recognition_run_id"], current["recognition_document_id"]),
            )
            connection.execute(
                """UPDATE topic_candidates SET status=%s, updated_at=CURRENT_TIMESTAMP
                   WHERE recognition_run_id=%s AND (
                       recognition_document_id=%s OR id IN (
                           SELECT topic_candidate_id FROM cross_validation_cases
                           WHERE recognition_run_id=%s AND recognition_document_id=%s
                       )
                   )""",
                (
                    candidate_status, current["recognition_run_id"], current["recognition_document_id"],
                    current["recognition_run_id"], current["recognition_document_id"],
                ),
            )
        return {"review": review, "decision": updated}

    def list_human_reviews(self, run_id: uuid.UUID) -> list[dict[str, Any]]:
        with connect(self.settings) as connection:
            return list(connection.execute(
                """SELECT h.* FROM human_reviews h
                   JOIN resource_decisions d ON d.id=h.resource_decision_id
                   WHERE d.recognition_run_id=%s ORDER BY h.created_at""",
                (run_id,),
            ).fetchall())

    def list_runs(self, corpus_id: uuid.UUID | None = None, limit: int = 20) -> list[dict[str, Any]]:
        with connect(self.settings) as connection:
            if corpus_id is None:
                return list(connection.execute(
                    """SELECT r.*, c.corpus_key FROM recognition_runs r
                       JOIN corpora c ON c.id=r.corpus_id
                       ORDER BY r.created_at DESC LIMIT %s""",
                    (limit,),
                ).fetchall())
            return list(connection.execute(
                "SELECT * FROM recognition_runs WHERE corpus_id=%s ORDER BY created_at DESC LIMIT %s",
                (corpus_id, limit),
            ).fetchall())

    def add_human_review(
        self,
        resource_decision_id: uuid.UUID,
        *,
        reviewer: str,
        action: str,
        before_state: Mapping[str, Any],
        after_state: Mapping[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        with connect(self.settings) as connection, connection.transaction():
            return connection.execute(
                """INSERT INTO human_reviews(
                       id, resource_decision_id, reviewer, action,
                       before_state, after_state, reason)
                   VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s) RETURNING *""",
                (
                    uuid.uuid4(), resource_decision_id, reviewer, action,
                    _json(before_state), _json(after_state), reason,
                ),
            ).fetchone()
