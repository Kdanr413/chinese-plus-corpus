"""Vendor-neutral validator boundary and one OpenAI-compatible transport adapter."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Protocol

from pydantic import ValidationError
from scripts.workflow.prompting import chat_completions_url

from .config import ValidatorModelConfig
from .errors import ValidatorResponseError, ValidatorUnavailableError
from .models import (
    CrossValidationRequest,
    TermValidationRequest,
    TopicValidationRequest,
    ValidatorResult,
)
from .prompts import PromptSnapshot
from .topic import DiscoveredCluster, TopicLabel


class ChatTransport(Protocol):
    def complete(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
        timeout_seconds: float,
    ) -> str: ...


class OpenAICompatibleChatTransport:
    """Small HTTP adapter; provider business rules do not depend on this class."""

    def complete(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
        timeout_seconds: float,
    ) -> str:
        body = json.dumps(
            {"model": model, "temperature": 0.1, "messages": messages},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            chat_completions_url(base_url),
            data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            content = payload.get("choices", [{}])[0].get("message", {}).get("content")
            if content is None or not str(content).strip():
                raise ValidatorResponseError("model response has empty message.content")
            return str(content)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ValidatorUnavailableError(f"validator HTTP {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise ValidatorUnavailableError(f"validator connection failed: {exc.reason}") from exc


@dataclass(frozen=True)
class ResolvedValidatorSettings:
    validator_id: str
    provider: str
    model: str
    model_version: str
    base_url: str
    api_key: str = field(repr=False)
    weight: float = 1.0


def resolve_validator_settings(
    config: ValidatorModelConfig,
    environment: Mapping[str, str] | None = None,
) -> ResolvedValidatorSettings:
    values = environment if environment is not None else os.environ

    def required(variable: str | None, label: str) -> str:
        value = values.get(variable or "", "").strip()
        if not variable or not value:
            raise ValidatorUnavailableError(
                f"validator {config.id} is missing {label} environment variable: {variable or '<not configured>'}"
            )
        return value

    model = required(config.model_env, "model")
    base_url = required(config.base_url_env, "base URL")
    api_key = required(config.api_key_env, "API key")
    model_version = values.get(config.model_version_env or "", "").strip() or model
    return ResolvedValidatorSettings(
        validator_id=config.id,
        provider=config.provider,
        model=model,
        model_version=model_version,
        base_url=base_url,
        api_key=api_key,
        weight=config.weight,
    )


def _json_object(content: str) -> dict[str, Any]:
    value = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", value, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        value = fenced.group(1)
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        # Some otherwise compatible models prepend a short explanation before
        # the JSON object. Decode the first object without accepting arbitrary
        # non-JSON output as a successful validation result.
        object_start = value.find("{")
        if object_start < 0:
            raise ValidatorResponseError(f"validator did not return valid JSON: {exc}") from exc
        try:
            payload, _ = json.JSONDecoder().raw_decode(value[object_start:])
        except json.JSONDecodeError as nested:
            raise ValidatorResponseError(f"validator did not return valid JSON: {nested}") from nested
    if not isinstance(payload, dict):
        raise ValidatorResponseError("validator JSON must be an object")
    if isinstance(payload.get("decision"), str):
        payload["decision"] = payload["decision"].strip().lower()
    if "reason" in payload and not isinstance(payload["reason"], str):
        payload["reason"] = json.dumps(payload["reason"], ensure_ascii=False)
    if "evidence" in payload and not isinstance(payload["evidence"], dict):
        payload["evidence"] = {"model_evidence": payload["evidence"]}
    return payload


class OpenAICompatibleValidatorProvider:
    """Term validator that can target any compatible endpoint through configuration."""

    version = "1.0"

    def __init__(
        self,
        settings: ResolvedValidatorSettings,
        prompt: PromptSnapshot,
        *,
        transport: ChatTransport | None = None,
        timeout_seconds: float = 180.0,
    ) -> None:
        self.settings = settings
        self.prompt = prompt
        self.transport = transport or OpenAICompatibleChatTransport()
        self.timeout_seconds = timeout_seconds
        self.name = settings.validator_id
        self.model = settings.model
        self.model_version = settings.model_version

    @classmethod
    def from_config(
        cls,
        config: ValidatorModelConfig,
        prompt: PromptSnapshot,
        *,
        environment: Mapping[str, str] | None = None,
        transport: ChatTransport | None = None,
    ) -> "OpenAICompatibleValidatorProvider":
        if config.provider != "openai_compatible":
            raise ValidatorUnavailableError(
                f"provider {config.provider!r} needs its own ValidatorProvider implementation"
            )
        return cls(resolve_validator_settings(config, environment), prompt, transport=transport)

    def validate_term(self, request: TermValidationRequest) -> ValidatorResult:
        template = self.prompt.template
        messages = [
            {"role": "system", "content": template.system_prompt},
            {
                "role": "user",
                "content": template.task_instruction + "\n\n输入：\n"
                + json.dumps(request.model_dump(mode="json"), ensure_ascii=False),
            },
        ]
        content = self.transport.complete(
            base_url=self.settings.base_url,
            api_key=self.settings.api_key,
            model=self.model,
            messages=messages,
            timeout_seconds=self.timeout_seconds,
        )
        payload = _json_object(content)
        try:
            return ValidatorResult(
                validator=self.name,
                validator_version=self.version,
                model=self.model,
                model_version=self.model_version,
                prompt_id=template.prompt_id,
                prompt_version=template.prompt_version,
                decision=payload.get("decision"),
                suggested_term=payload.get("suggested_term"),
                domain_relevance=payload.get("domain_relevance"),
                linguistic_integrity=payload.get("linguistic_integrity"),
                reason=payload.get("reason", ""),
                evidence=payload.get("evidence", {}),
            )
        except ValidationError as exc:
            raise ValidatorResponseError(f"validator JSON violates the term result contract: {exc}") from exc

    def validate_topic(self, request: TopicValidationRequest) -> ValidatorResult:
        template = self.prompt.template
        payload = self._complete_json(request.model_dump(mode="json"))
        try:
            return ValidatorResult(
                validator=self.name, validator_version=self.version,
                model=self.model, model_version=self.model_version,
                prompt_id=template.prompt_id, prompt_version=template.prompt_version,
                decision=payload.get("decision"),
                suggested_topic=payload.get("suggested_topic"),
                existing_topic_id=payload.get("existing_topic_id"),
                topic_fit=payload.get("topic_fit"),
                granularity_assessment=payload.get("granularity_assessment"),
                reason=payload.get("reason", ""), evidence={
                    **(payload.get("evidence") or {}),
                    "new_topic": bool(payload.get("new_topic", False)),
                    "resolution": payload.get("resolution"),
                    "topic_selection": payload.get("topic_selection") or {},
                    "path_valid": payload.get("path_valid"),
                    "hierarchy_issues": payload.get("hierarchy_issues") or [],
                },
            )
        except ValidationError as exc:
            raise ValidatorResponseError(f"validator JSON violates the topic result contract: {exc}") from exc

    def cross_validate(self, request: CrossValidationRequest) -> ValidatorResult:
        template = self.prompt.template
        payload = self._complete_json(request.model_dump(mode="json"))
        try:
            evidence = dict(payload.get("evidence") or {})
            for key in (
                "term_supports_topic", "topic_supports_terms", "conflicting_terms",
                "missing_expected_terms", "possible_topic_revision", "possible_term_revision",
            ):
                if key in payload:
                    evidence[key] = payload[key]
            return ValidatorResult(
                validator=self.name, validator_version=self.version,
                model=self.model, model_version=self.model_version,
                prompt_id=template.prompt_id, prompt_version=template.prompt_version,
                decision=payload.get("decision"),
                semantic_consistency=payload.get("semantic_consistency"),
                reason=payload.get("reason", ""), evidence=evidence,
            )
        except ValidationError as exc:
            raise ValidatorResponseError(f"validator JSON violates the cross result contract: {exc}") from exc

    def _complete_json(self, value: Mapping[str, Any]) -> dict[str, Any]:
        template = self.prompt.template
        content = self.transport.complete(
            base_url=self.settings.base_url,
            api_key=self.settings.api_key,
            model=self.model,
            messages=[
                {"role": "system", "content": template.system_prompt},
                {"role": "user", "content": template.task_instruction + "\n\n输入：\n" + json.dumps(value, ensure_ascii=False)},
            ],
            timeout_seconds=self.timeout_seconds,
        )
        return _json_object(content)


class OpenAICompatibleTopicLabelProvider:
    """Optional discovery labeler; absence of this provider never blocks clustering."""

    version = "1.0"

    def __init__(
        self,
        settings: ResolvedValidatorSettings,
        prompt: PromptSnapshot,
        *,
        transport: ChatTransport | None = None,
        timeout_seconds: float = 180.0,
    ) -> None:
        self.settings = settings
        self.prompt = prompt
        self.transport = transport or OpenAICompatibleChatTransport()
        self.timeout_seconds = timeout_seconds
        self.name = settings.validator_id
        self.model = settings.model
        self.model_version = settings.model_version

    def label_topic(self, cluster: DiscoveredCluster) -> TopicLabel:
        template = self.prompt.template
        content = self.transport.complete(
            base_url=self.settings.base_url,
            api_key=self.settings.api_key,
            model=self.model,
            messages=[
                {"role": "system", "content": template.system_prompt},
                {
                    "role": "user",
                    "content": template.task_instruction + "\n\n输入：\n"
                    + json.dumps(asdict(cluster), ensure_ascii=False),
                },
            ],
            timeout_seconds=self.timeout_seconds,
        )
        payload = _json_object(content)
        topic_name = str(payload.get("topic_name") or "").strip()
        topic_definition = str(payload.get("topic_definition") or "").strip()
        if not topic_name or not topic_definition:
            raise ValidatorResponseError("topic label JSON requires topic_name and topic_definition")
        return TopicLabel(
            topic_name=topic_name,
            topic_definition=topic_definition,
            evidence={
                "label_status": "model_generated_candidate",
                "provider": self.name,
                "provider_version": self.version,
                "model": self.model,
                "model_version": self.model_version,
                "prompt_id": template.prompt_id,
                "prompt_version": template.prompt_version,
                "prompt_checksum_sha256": self.prompt.checksum_sha256,
                "reason": str(payload.get("reason") or "").strip(),
                "evidence": payload.get("evidence") or {},
            },
        )


class BuiltinRuleValidatorProvider:
    """Offline, explainable baseline so the workflow never requires a paid model."""

    name = "builtin_rule_baseline"
    version = "1.0"
    model = "deterministic-rules"
    model_version = "1.0"

    def __init__(
        self,
        term_prompt: PromptSnapshot,
        topic_prompt: PromptSnapshot,
        cross_prompt: PromptSnapshot,
    ) -> None:
        self.term_prompt = term_prompt
        self.topic_prompt = topic_prompt
        self.cross_prompt = cross_prompt

    def validate_term(self, request: TermValidationRequest) -> ValidatorResult:
        term = request.candidate.normalized_form.strip()
        common = {"今天", "公司", "进行", "有关", "相关", "问题", "工作", "可以"}
        decision = "reject" if term in common or len(term) < 2 else "accept"
        reason = "候选是普通表达或长度不足" if decision == "reject" else "候选在原句中完整出现且具备可复核的名词性表达"
        return self._result(
            self.term_prompt, decision, reason,
            evidence={"sentence": request.candidate.sentence, "rule": "surface_integrity_and_stoplist"},
            suggested_term=term,
            domain_relevance=0.6 if decision == "accept" else 0.1,
            linguistic_integrity=1.0 if term in request.document.clean_text else 0.0,
        )

    def validate_topic(self, request: TopicValidationRequest) -> ValidatorResult:
        resolution = request.candidate.resolution.value
        path = str(request.candidate.evidence.get("path") or "")
        levels = [part.strip() for part in path.split(">") if part.strip()]
        if resolution == "existing":
            decision, reason = "accept", "候选由非空话题表匹配产生，保留为可复核的已有话题映射"
        elif resolution == "new_topic":
            decision, reason = "review", "新话题的名称、边界和粒度需要人工或模型独立复核"
        else:
            decision, reason = "review", "文本未匹配已有话题，需要人工判断是否为新话题"
        return self._result(
            self.topic_prompt, decision, reason,
            evidence={
                "resolution": resolution,
                "new_topic": resolution == "new_topic",
                "topic_selection": {
                    f"level_{index}": {"topic_name": name, "source": "matched_path"}
                    for index, name in enumerate(levels[:3], start=1)
                },
                "path_valid": bool(path) if resolution == "existing" else None,
                "hierarchy_issues": [],
            },
            suggested_topic=request.candidate.topic_name,
            existing_topic_id=request.candidate.evidence.get("mapped_topic_id"),
            topic_fit=0.8 if decision == "accept" else 0.4,
            granularity_assessment="requires_review" if decision != "accept" else "appropriate",
        )

    def cross_validate(self, request: CrossValidationRequest) -> ValidatorResult:
        topic_terms = set(request.validated_topic.keywords)
        topic_terms.update(part.strip() for part in request.validated_topic.topic_name.split("/") if part.strip())
        term_values = {term.normalized_form for term in request.validated_terms}
        supported = sorted(term for term in term_values if any(term in value or value in term for value in topic_terms))
        if supported:
            decision, consistency = "accept", 1.0
            reason = "候选术语与话题名称或关键词存在明确语义词面支撑"
        elif term_values:
            decision, consistency = "review", 0.4
            reason = "术语与话题缺少可解释的直接支撑，需要人工复核"
        else:
            decision, consistency = "review", 0.0
            reason = "当前文档没有可用于交叉验证的术语"
        return self._result(
            self.cross_prompt, decision, reason,
            evidence={
                "term_supports_topic": bool(supported),
                "topic_supports_terms": bool(supported),
                "supported_terms": supported,
                "conflicting_terms": [] if supported else sorted(term_values),
                "missing_expected_terms": sorted(topic_terms - term_values)[:20],
                "possible_topic_revision": None,
                "possible_term_revision": {},
            },
            semantic_consistency=consistency,
        )

    def _result(
        self, prompt: PromptSnapshot, decision: str, reason: str,
        *, evidence: Mapping[str, Any], **values: Any,
    ) -> ValidatorResult:
        template = prompt.template
        return ValidatorResult(
            validator=self.name, validator_version=self.version,
            model=self.model, model_version=self.model_version,
            prompt_id=template.prompt_id, prompt_version=template.prompt_version,
            decision=decision, reason=reason, evidence=dict(evidence), **values,
        )
