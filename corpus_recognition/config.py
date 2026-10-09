"""Validated recognition configuration without embedded credentials."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import RecognitionConfigurationError
from .models import AggregationStrategy


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "configs" / "recognition.json"


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExtractorConfig(ConfigModel):
    id: str
    enabled: bool = True
    options: dict = Field(default_factory=dict)


class TermExtractionConfig(ConfigModel):
    extractors: list[ExtractorConfig] = Field(min_length=1)


class TopicIdentificationConfig(ConfigModel):
    mode: Literal["auto", "classification", "discovery"] = "auto"
    unknown_enabled: bool = True
    new_topic_enabled: bool = True


class AnalysisUnitConfig(ConfigModel):
    narrative: Literal["sentence"] = "sentence"
    dialogue: Literal["dialogue_turn"] = "dialogue_turn"
    term_candidates_per_unit: int = Field(default=12, ge=1, le=100)


class ValidatorModelConfig(ConfigModel):
    id: str
    provider: str
    model_env: str
    model_version_env: str | None = None
    base_url_env: str | None = None
    api_key_env: str | None = None
    weight: float = Field(default=1.0, gt=0)
    enabled: bool = True

    @model_validator(mode="after")
    def reject_secret_values(self) -> "ValidatorModelConfig":
        for field_name in ("model_env", "model_version_env", "base_url_env", "api_key_env"):
            value = getattr(self, field_name)
            if value and ("/" in value or "://" in value or " " in value):
                raise ValueError(f"{field_name} must name an environment variable, not contain a value")
        return self


class ValidationConfig(ConfigModel):
    models: list[ValidatorModelConfig]
    strategy: AggregationStrategy = AggregationStrategy.MAJORITY
    min_validators: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_enabled_count(self) -> "ValidationConfig":
        enabled = sum(model.enabled for model in self.models)
        if enabled < self.min_validators:
            raise ValueError("enabled validator count must be at least min_validators")
        return self


class StageConfig(ConfigModel):
    enabled: bool = True
    prompt: str


class DecisionConfig(ConfigModel):
    auto_accept_rule: str
    manual_review_rule: str


class RecognitionConfig(ConfigModel):
    schema_version: str
    analysis_units: AnalysisUnitConfig = Field(default_factory=AnalysisUnitConfig)
    term_extraction: TermExtractionConfig
    topic_identification: TopicIdentificationConfig
    topic_discovery_naming: StageConfig
    validation: ValidationConfig
    term_validation: StageConfig
    term_model_extraction: StageConfig
    topic_validation: StageConfig
    cross_validation: StageConfig
    decision: DecisionConfig


def load_recognition_config(path: str | Path = DEFAULT_CONFIG_PATH) -> RecognitionConfig:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
        return RecognitionConfig.model_validate(payload)
    except (OSError, ValueError) as exc:
        raise RecognitionConfigurationError(f"invalid recognition config {source}: {exc}") from exc
