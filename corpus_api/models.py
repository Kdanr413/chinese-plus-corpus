"""JSON response schemas exposed by the stage 6 API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, JsonValue


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HealthResponse(ApiModel):
    status: str
    service: str
    api_version: str


class CorpusSummary(ApiModel):
    id: uuid.UUID
    corpus_key: str
    name: str
    description: str
    status: str
    default_version_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class CorpusListResponse(ApiModel):
    items: list[CorpusSummary]
    count: int


class CorpusVersionSummary(ApiModel):
    id: uuid.UUID
    corpus_id: uuid.UUID
    version: str
    status: str
    searchable: bool
    parent_version_id: uuid.UUID | None
    release_path: str
    corpus_format_version: str
    pipeline_version: str
    search_engine_version: str
    checksum_sha256: str
    created_at: datetime
    updated_at: datetime


class CorpusVersionListResponse(ApiModel):
    corpus_id: uuid.UUID
    items: list[CorpusVersionSummary]
    count: int


class SearchResponse(ApiModel):
    corpus_id: uuid.UUID
    corpus_key: str
    corpus_name: str
    version_id: uuid.UUID
    version: str
    query: str
    command: str
    duration_ms: int
    result: JsonValue


class ErrorDetail(ApiModel):
    code: str
    message: str


class ErrorResponse(ApiModel):
    error: ErrorDetail
