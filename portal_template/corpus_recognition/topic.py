"""Independent topic candidates with automatic classification/discovery routing."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from .models import Document, TopicCandidate, TopicResolution
from .repository import RecognitionRepository


def _stable_id(prefix: str, *values: str) -> str:
    digest = hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.replace("|", ",").split(",") if item.strip()]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


@dataclass(frozen=True)
class TopicNode:
    topic_id: str
    topic_name: str
    parent_topic_id: str = ""
    definition: str = ""
    keywords: tuple[str, ...] = ()
    typical_tasks: tuple[str, ...] = ()
    typical_scenarios: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    path: str = ""


def _node_from_mapping(row: Mapping[str, Any], *, path: str = "") -> TopicNode | None:
    topic_id = str(row.get("topic_id") or row.get("id") or "").strip()
    topic_name = str(row.get("topic_name") or row.get("name") or "").strip()
    if not topic_id or not topic_name:
        return None
    return TopicNode(
        topic_id=topic_id,
        topic_name=topic_name,
        parent_topic_id=str(row.get("parent_topic_id") or row.get("parent_id") or "").strip(),
        definition=str(row.get("definition") or "").strip(),
        keywords=tuple(_strings(row.get("keywords") or row.get("representative_keywords"))),
        typical_tasks=tuple(_strings(row.get("typical_tasks"))),
        typical_scenarios=tuple(_strings(row.get("typical_scenarios"))),
        examples=tuple(_strings(row.get("examples"))),
        path=str(row.get("path") or path or topic_name).strip(),
    )


def normalize_topic_table(value: Any) -> list[TopicNode]:
    """Accept the current curated taxonomy, a planned flat table, or an empty value."""
    if value is None:
        return []
    if isinstance(value, (str, Path)):
        source = Path(value)
        if not source.exists():
            return []
        if source.suffix.lower() == ".csv":
            with source.open("r", encoding="utf-8-sig", newline="") as handle:
                value = list(csv.DictReader(handle))
        else:
            value = json.loads(source.read_text(encoding="utf-8-sig"))

    rows: list[TopicNode] = []
    if isinstance(value, Mapping):
        flat = value.get("nodes")
        if isinstance(flat, list):
            rows.extend(node for item in flat if isinstance(item, Mapping) if (node := _node_from_mapping(item)))

        def visit(item: Mapping[str, Any], parent_id: str = "", names: tuple[str, ...] = ()) -> None:
            name = str(item.get("topic_name") or item.get("name") or "").strip()
            current_names = (*names, name) if name else names
            normalized = _node_from_mapping(
                {**item, "parent_topic_id": item.get("parent_topic_id") or item.get("parent_id") or parent_id},
                path=" > ".join(current_names),
            )
            if normalized:
                rows.append(normalized)
                parent_id = normalized.topic_id
            for child in item.get("children", []) or []:
                if isinstance(child, Mapping):
                    visit(child, parent_id, current_names)

        root = value.get("root")
        if isinstance(root, Mapping):
            visit(root)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        rows.extend(node for item in value if isinstance(item, Mapping) if (node := _node_from_mapping(item)))
    else:
        raise ValueError("topic table must be a JSON object, row list, CSV/JSON path, or empty")

    unique: dict[str, TopicNode] = {}
    for node in rows:
        unique[node.topic_id] = node
    return list(unique.values())


class ConstrainedTopicClassifier:
    """Produce an existing-topic candidate or explicit UNKNOWN for each document."""

    name = "keyword_constrained_classification"
    version = "1.0"

    def identify(
        self,
        documents: Sequence[Document],
        topic_table: Sequence[TopicNode],
        *,
        progress_callback: Callable[[int, int], None] | None = None,
        checkpoint_callback: Callable[[], None] | None = None,
    ) -> list[TopicCandidate]:
        if not topic_table:
            raise ValueError("constrained classification requires a non-empty topic table")
        candidates: list[TopicCandidate] = []
        total = len(documents)
        for index, document in enumerate(documents, start=1):
            if checkpoint_callback is not None:
                checkpoint_callback()
            text = document.clean_text.casefold()
            scored: list[tuple[float, int, TopicNode, dict[str, Any]]] = []
            for node in topic_table:
                name_hits = text.count(node.topic_name.casefold()) if node.topic_name else 0
                keyword_hits = {
                    word: text.count(word.casefold())
                    for word in node.keywords
                    if word and text.count(word.casefold())
                }
                score = name_hits * 3.0 + float(sum(keyword_hits.values()))
                if score:
                    depth = max(1, node.path.count(">") + 1)
                    scored.append((score, depth, node, {"name_hits": name_hits, "keyword_hits": keyword_hits}))
            if not scored:
                candidates.append(TopicCandidate(
                    topic_candidate_id=_stable_id("topic_unknown", document.document_id),
                    document_id=document.document_id,
                    topic_name="UNKNOWN",
                    source_method=self.name,
                    resolution=TopicResolution.UNKNOWN,
                    evidence={
                        "reason": "no existing topic name or keyword matched",
                        "topic_table_size": len(topic_table),
                        "classifier_version": self.version,
                    },
                ))
                if progress_callback is not None and (index == total or index % 500 == 0):
                    progress_callback(index, total)
                continue
            score, depth, node, match = max(
                scored, key=lambda item: (item[0], item[1], item[2].topic_id)
            )
            candidates.append(TopicCandidate(
                topic_candidate_id=_stable_id("topic_existing", document.document_id, node.topic_id),
                document_id=document.document_id,
                topic_name=node.topic_name,
                topic_definition=node.definition,
                keywords=list(node.keywords),
                source_method=self.name,
                resolution=TopicResolution.EXISTING,
                evidence={
                    "mapped_topic_id": node.topic_id,
                    "parent_topic_id": node.parent_topic_id,
                    "path": node.path,
                    "match_score": score,
                    "match": match,
                    "classifier_version": self.version,
                },
            ))
            if progress_callback is not None and (index == total or index % 500 == 0):
                progress_callback(index, total)
        return candidates


class ContentUnitTopicIdentifier:
    """Identify one candidate per sentence/dialogue turn from its own text evidence."""

    name = "content_unit_topic_identification"
    version = "1.0"

    def __init__(self, keyword_extractor: Any, classifier: ConstrainedTopicClassifier | None = None):
        self.keyword_extractor = keyword_extractor
        self.classifier = classifier or ConstrainedTopicClassifier()

    def identify(
        self,
        documents: Sequence[Document],
        topic_table: Any = None,
        *,
        progress_callback: Callable[[int, int], None] | None = None,
        checkpoint_callback: Callable[[], None] | None = None,
    ) -> list[TopicCandidate]:
        nodes = normalize_topic_table(topic_table)
        if nodes:
            return self.classifier.identify(
                documents,
                nodes,
                progress_callback=progress_callback,
                checkpoint_callback=checkpoint_callback,
            )
        if topic_table:
            raise ValueError(
                "已配置话题体系文件，但没有解析出有效节点；为防止误用无约束候选发现，流程已停止"
            )
        candidates: list[TopicCandidate] = []
        total = len(documents)
        for index, document in enumerate(documents, start=1):
            if checkpoint_callback is not None:
                checkpoint_callback()
            extracted = self.keyword_extractor.extract(document)
            keywords: list[str] = []
            for row in sorted(extracted, key=lambda item: item.score or 0.0, reverse=True):
                value = row.normalized_form.strip()
                if value and value not in keywords:
                    keywords.append(value)
                if len(keywords) >= 5:
                    break
            inherited = [
                value for value in _strings(document.metadata.get("topics_auto"))
                if value in document.clean_text and value not in keywords
            ]
            keywords.extend(inherited[:3])
            topic_name = " / ".join(keywords[:3])
            if not topic_name:
                topic_name = f"待验证：{document.clean_text[:28].strip()}"
            unit_type = str(document.metadata.get("analysis_unit_type") or "text_unit")
            candidates.append(TopicCandidate(
                topic_candidate_id=_stable_id("topic_unit", document.document_id, topic_name),
                document_id=document.document_id,
                topic_name=topic_name,
                topic_definition="根据当前句子或完整对话话轮中的词汇证据生成，等待独立话题验证。",
                keywords=keywords,
                representative_texts=[document.clean_text],
                source_method=self.name,
                resolution=TopicResolution.NEW_TOPIC,
                evidence={
                    "document_ids": [document.document_id],
                    "analysis_unit_type": unit_type,
                    "source_document_id": document.metadata.get("source_document_id", document.document_id),
                    "keyword_evidence": keywords,
                    "title_used_as_fallback": False,
                    "requires_name_validation": True,
                },
            ))
            if progress_callback is not None and (index == total or index % 500 == 0):
                progress_callback(index, total)
        return candidates


@dataclass(frozen=True)
class DiscoveredCluster:
    cluster_id: str
    document_ids: tuple[str, ...]
    topic_name: str
    topic_definition: str = ""
    keywords: tuple[str, ...] = ()
    representative_texts: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)


class TopicDiscoveryBackend(Protocol):
    name: str
    version: str

    def discover(self, documents: Sequence[Document]) -> tuple[list[DiscoveredCluster], list[str]]: ...


@dataclass(frozen=True)
class TopicLabel:
    topic_name: str
    topic_definition: str
    evidence: Mapping[str, Any] = field(default_factory=dict)


class TopicLabelProvider(Protocol):
    name: str
    version: str

    def label_topic(self, cluster: DiscoveredCluster) -> TopicLabel: ...


class SingleDocumentTopicDiscoveryBackend:
    """Create an auditable candidate when clustering is impossible for one document.

    This is deliberately a candidate generator, not a classifier.  It uses the
    document title and the cleaning-stage topic hints only to seed independent
    topic validation; it never publishes a topic directly.
    """

    name = "single_document_metadata_keywords"
    version = "1.0"

    def discover(self, documents: Sequence[Document]) -> tuple[list[DiscoveredCluster], list[str]]:
        if len(documents) != 1:
            raise ValueError("single-document discovery requires exactly one document")
        document = documents[0]
        title = str(document.metadata.get("title") or "").strip()
        topic_hints = _strings(document.metadata.get("topics_auto"))
        keywords = tuple(dict.fromkeys([*topic_hints, title] if title else topic_hints))
        meaningful_hints = topic_hints[:3]
        topic_name = " / ".join(meaningful_hints) or title[:80] or "待验证单文档话题"
        definition = (
            "由单篇文档的标题与清洗阶段话题提示生成的候选话题；"
            "由于单文档无法进行语义聚类，名称、层级与边界必须继续独立验证。"
        )
        return ([DiscoveredCluster(
            cluster_id=f"SINGLE_{document.document_id}",
            document_ids=(document.document_id,),
            topic_name=topic_name,
            topic_definition=definition,
            keywords=keywords[:20],
            representative_texts=(document.clean_text[:500],),
            evidence={
                "algorithm": self.name,
                "document_count": 1,
                "title": title,
                "topic_hints": topic_hints,
                "clustering_skipped": True,
                "reason": "语义聚类至少需要两篇文档，已切换到单文档候选生成方法",
                "requires_name_validation": True,
            },
        )], [])


class SmallCorpusTopicDiscoveryBackend(SingleDocumentTopicDiscoveryBackend):
    """Generate separate, reviewable topic candidates for a two-document corpus."""

    name = "small_corpus_metadata_keywords"
    version = "1.0"

    def discover(self, documents: Sequence[Document]) -> tuple[list[DiscoveredCluster], list[str]]:
        if not 1 <= len(documents) <= 2:
            raise ValueError("small-corpus discovery supports one or two documents")
        clusters = []
        for document in documents:
            document_clusters, _ = super().discover([document])
            cluster = document_clusters[0]
            clusters.append(DiscoveredCluster(
                cluster_id=cluster.cluster_id,
                document_ids=cluster.document_ids,
                topic_name=cluster.topic_name,
                topic_definition=cluster.topic_definition,
                keywords=cluster.keywords,
                representative_texts=cluster.representative_texts,
                evidence={
                    **dict(cluster.evidence),
                    "corpus_document_count": len(documents),
                    "reason": "当前语料仅有一至两篇，不满足稳定聚类条件，已逐文档生成候选话题",
                },
            ))
        return clusters, []


class LegacyBertopicDiscoveryBackend:
    """Reuse the existing BERTopic runner without its reference-table or publishing side effects."""

    name = "legacy_bertopic"
    version = "1.0"

    def __init__(self, config: str | Path | Mapping[str, Any]):
        if isinstance(config, Mapping):
            self.config = dict(config)
        else:
            self.config = json.loads(Path(config).read_text(encoding="utf-8-sig"))

    def discover(self, documents: Sequence[Document]) -> tuple[list[DiscoveredCluster], list[str]]:
        if len(documents) < 2:
            raise ValueError("BERTopic discovery requires at least two documents")
        from scripts.topics.taxonomy_workflow import run_model

        model, assignments, probabilities = run_model(
            [document.clean_text for document in documents],
            self.config,
            int(self.config.get("random_seed", 20260817)),
        )
        grouped: dict[int, list[int]] = defaultdict(list)
        for index, topic_id in enumerate(assignments):
            grouped[int(topic_id)].append(index)
        clusters: list[DiscoveredCluster] = []
        outliers: list[str] = []
        top_n = int(self.config.get("representation", {}).get("top_words", 20))
        for topic_id, indexes in sorted(grouped.items()):
            if topic_id == -1:
                outliers.extend(documents[index].document_id for index in indexes)
                continue
            raw_words = model.get_topic(topic_id) or []
            keywords = tuple(str(word).strip() for word, _ in raw_words[:top_n] if str(word).strip())
            meaningful = [word for word in keywords if any(char.isalnum() for char in word)]
            name = " / ".join(meaningful[:3]) or f"待命名话题 {topic_id}"
            sample_indexes = indexes[: min(5, len(indexes))]
            clusters.append(DiscoveredCluster(
                cluster_id=f"BT_TOPIC_{topic_id:03d}",
                document_ids=tuple(documents[index].document_id for index in indexes),
                topic_name=name,
                topic_definition="BERTopic 生成的候选话题，名称和边界仍需独立验证与人工复核。",
                keywords=keywords,
                representative_texts=tuple(documents[index].clean_text[:500] for index in sample_indexes),
                evidence={
                    "bertopic_id": topic_id,
                    "document_count": len(indexes),
                    "probabilities_available": probabilities is not None,
                    "requires_name_validation": True,
                },
            ))
        return clusters, outliers


class DiscoveryTopicIdentifier:
    name = "topic_discovery"
    version = "1.0"

    def __init__(
        self,
        backend: TopicDiscoveryBackend,
        label_provider: TopicLabelProvider | None = None,
        single_document_backend: TopicDiscoveryBackend | None = None,
        small_corpus_backend: TopicDiscoveryBackend | None = None,
    ):
        self.backend = backend
        self.label_provider = label_provider
        self.single_document_backend = single_document_backend
        self.small_corpus_backend = small_corpus_backend

    def identify(self, documents: Sequence[Document]) -> list[TopicCandidate]:
        active_backend = (
            self.small_corpus_backend
            if 1 <= len(documents) <= 2 and self.small_corpus_backend is not None
            else
            self.single_document_backend
            if len(documents) == 1 and self.single_document_backend is not None
            else self.backend
        )
        clusters, outlier_ids = active_backend.discover(documents)
        candidates = []
        for cluster in clusters:
            label = self.label_provider.label_topic(cluster) if self.label_provider else TopicLabel(
                topic_name=cluster.topic_name,
                topic_definition=cluster.topic_definition,
                evidence={"label_status": "backend_fallback", "requires_name_validation": True},
            )
            candidates.append(TopicCandidate(
                topic_candidate_id=_stable_id("topic_new", cluster.cluster_id, *cluster.document_ids),
                cluster_id=cluster.cluster_id,
                topic_name=label.topic_name,
                topic_definition=label.topic_definition,
                keywords=list(cluster.keywords),
                representative_texts=list(cluster.representative_texts),
                source_method=active_backend.name,
                resolution=TopicResolution.NEW_TOPIC,
                evidence={
                    **dict(cluster.evidence),
                    "document_ids": list(cluster.document_ids),
                    "backend_version": active_backend.version,
                    "label": dict(label.evidence),
                },
            ))
        by_id = {document.document_id: document for document in documents}
        for document_id in outlier_ids:
            document = by_id[document_id]
            candidates.append(TopicCandidate(
                topic_candidate_id=_stable_id("topic_unknown", document_id, active_backend.name),
                document_id=document_id,
                topic_name="UNKNOWN",
                source_method=active_backend.name,
                resolution=TopicResolution.UNKNOWN,
                representative_texts=[document.clean_text[:500]],
                evidence={"reason": "discovery backend marked the document as an outlier"},
            ))
        return candidates


class AutoTopicIdentifier:
    """Route by explicit mode, or by whether a normalized topic table is empty."""

    name = "auto_topic_identifier"
    version = "1.0"

    def __init__(
        self,
        discovery: DiscoveryTopicIdentifier,
        classifier: ConstrainedTopicClassifier | None = None,
        *,
        mode: str = "auto",
    ) -> None:
        if mode not in {"auto", "classification", "discovery"}:
            raise ValueError("mode must be auto, classification or discovery")
        self.discovery = discovery
        self.classifier = classifier or ConstrainedTopicClassifier()
        self.mode = mode

    def identify(self, documents: Sequence[Document], topic_table: Any = None) -> list[TopicCandidate]:
        nodes = normalize_topic_table(topic_table)
        selected = self.mode
        if selected == "auto":
            selected = "classification" if nodes else "discovery"
        if selected == "classification":
            return self.classifier.identify(documents, nodes)
        return self.discovery.identify(documents)


def persist_topic_candidates(
    repository: RecognitionRepository,
    recognition_run_id: Any,
    candidates: Sequence[TopicCandidate],
    *,
    document_row_ids: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Persist candidates as candidates; this function never publishes a taxonomy."""
    row_ids = document_row_ids or {}
    stored = []
    for candidate in candidates:
        document_row_id = row_ids.get(candidate.document_id) if candidate.document_id else None
        if candidate.document_id and document_row_id is None:
            raise ValueError(f"missing database document row id for {candidate.document_id}")
        stored.append(repository.add_topic_candidate(
            recognition_run_id,
            candidate_topic_id=candidate.topic_candidate_id,
            topic_name=candidate.topic_name,
            source_method=candidate.source_method,
            resolution=candidate.resolution.value,
            document_row_id=document_row_id,
            cluster_id=candidate.cluster_id,
            topic_definition=candidate.topic_definition,
            keywords=candidate.keywords,
            representative_texts=candidate.representative_texts,
            evidence=candidate.evidence,
        ))
    return stored
