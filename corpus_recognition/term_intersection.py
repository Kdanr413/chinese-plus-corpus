"""Sentence-level model term extraction intersected with the evaluated baseline."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .errors import ValidatorResponseError
from .models import Document, TermCandidate, TermValidationRequest, ValidatorResult
from .prompts import PromptSnapshot
from .providers import OpenAICompatibleChatTransport, ResolvedValidatorSettings


def _json_payload(content: str) -> dict[str, Any]:
    value = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", value, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        value = fenced.group(1)
    start = value.find("{")
    if start < 0:
        raise ValidatorResponseError("术语识别模型没有返回 JSON 对象")
    try:
        payload, _ = json.JSONDecoder().raw_decode(value[start:])
    except json.JSONDecodeError as exc:
        raise ValidatorResponseError(f"术语识别模型 JSON 无效：{exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("terms"), list):
        raise ValidatorResponseError("术语识别模型必须返回包含 terms 数组的 JSON 对象")
    return payload


def _term_values(payload: Mapping[str, Any]) -> tuple[set[str], dict[str, Any]]:
    values: set[str] = set()
    evidence: dict[str, Any] = {}
    for row in payload.get("terms", []):
        if isinstance(row, str):
            term, item_evidence = row.strip(), ""
        elif isinstance(row, Mapping):
            term = str(row.get("term") or row.get("normalized_form") or "").strip()
            item_evidence = row.get("evidence") or row.get("reason") or ""
        else:
            continue
        if term:
            values.add(term)
            evidence[term] = item_evidence
    return values, evidence


class EvaluatedBaselineTermProvider:
    """Accept the verified algorithm's baseline when no external model is requested."""

    name = "evaluated_term_baseline"
    version = "1.0"
    model = "grammar-bcc-evidence"
    model_version = "01_术语识别"

    def __init__(self, prompt: PromptSnapshot) -> None:
        self.prompt = prompt

    def validate_term(self, request: TermValidationRequest) -> ValidatorResult:
        template = self.prompt.template
        return ValidatorResult(
            validator=self.name, validator_version=self.version,
            model=self.model, model_version=self.model_version,
            prompt_id=template.prompt_id, prompt_version=template.prompt_version,
            decision="accept", suggested_term=request.candidate.normalized_form,
            reason="未启用外部大模型，直接采用经过效果核验的术语基础识别结果",
            evidence={
                "policy": "evaluated_baseline_direct_output",
                "extractor": request.candidate.extractor,
                "candidate_evidence": request.candidate.evidence,
            },
        )


class SentenceTermIntersectionProvider:
    """Extract once per sentence, then validate baseline candidates by exact intersection."""

    version = "1.0"

    def __init__(
        self,
        settings: ResolvedValidatorSettings,
        prompt: PromptSnapshot,
        documents: Sequence[Document],
        *,
        transport: OpenAICompatibleChatTransport | None = None,
        timeout_seconds: float = 180.0,
        progress_callback: Callable[[int, int, str], None] | None = None,
        checkpoint_callback: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.prompt = prompt
        self.transport = transport or OpenAICompatibleChatTransport()
        self.timeout_seconds = timeout_seconds
        self.name = settings.validator_id
        self.model = settings.model
        self.model_version = settings.model_version
        self.terms_by_document: dict[str, set[str]] = {}
        self.evidence_by_document: dict[str, dict[str, Any]] = {}
        checkpoint = checkpoint_callback or (lambda: None)
        for index, document in enumerate(documents, start=1):
            checkpoint()
            payload = self._extract(document)
            terms, evidence = _term_values(payload)
            self.terms_by_document[document.document_id] = terms
            self.evidence_by_document[document.document_id] = {
                "model_terms": sorted(terms), "term_evidence": evidence,
                "model_reason": str(payload.get("reason") or ""),
            }
            if progress_callback:
                progress_callback(index, len(documents), self.name)

    def _extract(self, document: Document) -> dict[str, Any]:
        template = self.prompt.template
        content = self.transport.complete(
            base_url=self.settings.base_url,
            api_key=self.settings.api_key,
            model=self.settings.model,
            messages=[
                {"role": "system", "content": template.system_prompt},
                {
                    "role": "user",
                    "content": template.task_instruction + "\n\n输入：\n" + json.dumps({
                        "document_id": document.document_id,
                        "sentence": document.clean_text,
                        "source": document.source,
                        "analysis_unit_type": document.metadata.get("analysis_unit_type"),
                    }, ensure_ascii=False),
                },
            ],
            timeout_seconds=self.timeout_seconds,
        )
        return _json_payload(content)

    def validate_term(self, request: TermValidationRequest) -> ValidatorResult:
        candidate: TermCandidate = request.candidate
        model_terms = self.terms_by_document.get(candidate.document_id, set())
        normalized = candidate.normalized_form.strip()
        accepted = normalized in model_terms or candidate.surface_form.strip() in model_terms
        template = self.prompt.template
        return ValidatorResult(
            validator=self.name, validator_version=self.version,
            model=self.model, model_version=self.model_version,
            prompt_id=template.prompt_id, prompt_version=template.prompt_version,
            decision="accept" if accepted else "reject",
            suggested_term=normalized if accepted else None,
            reason=(
                "术语基础表候选同时出现在模型逐句识别结果中"
                if accepted else "候选只存在于术语基础表，未进入该模型的逐句识别结果"
            ),
            evidence={
                "policy": "model_sentence_terms_intersect_evaluated_baseline",
                "baseline_term": normalized,
                **self.evidence_by_document.get(candidate.document_id, {}),
            },
        )


__all__ = ["EvaluatedBaselineTermProvider", "SentenceTermIntersectionProvider"]
