"""Command-line access to the independent recognition contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .documents import load_documents_jsonl, write_term_candidates_jsonl, write_topic_candidates_jsonl
from .term import (
    CompositeTermExtractor,
    KnowledgeSeedTermExtractor,
    LegacyLlmConsensusTermExtractor,
    LegacyStatisticalTermExtractor,
)
from .topic import (
    AutoTopicIdentifier,
    DiscoveryTopicIdentifier,
    LegacyBertopicDiscoveryBackend,
    SmallCorpusTopicDiscoveryBackend,
)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python -m corpus_recognition")
    commands = root.add_subparsers(dest="command", required=True)
    extract = commands.add_parser("extract-terms", help="independent term candidate extraction")
    extract.add_argument("--documents", type=Path, required=True)
    extract.add_argument("--output", type=Path, required=True)
    extract.add_argument("--config", type=Path, default=Path("configs/term_extraction.json"))
    extract.add_argument("--processing-version", default="legacy-jsonl/1.0")
    extract.add_argument("--stop-terms", type=Path)
    extract.add_argument("--knowledge-csv", type=Path)
    extract.add_argument("--llm-results", type=Path)
    extract.add_argument("--sentences", type=Path)
    extract.add_argument("--llm-minimum-votes", type=int, default=2)
    topics = commands.add_parser("identify-topics", help="auto classification/discovery topic candidates")
    topics.add_argument("--documents", type=Path, required=True)
    topics.add_argument("--output", type=Path, required=True)
    topics.add_argument("--topic-table", type=Path)
    topics.add_argument("--bertopic-config", type=Path, default=Path("configs/topics/bertopic.json"))
    topics.add_argument("--mode", choices=("auto", "classification", "discovery"), default="auto")
    topics.add_argument("--processing-version", default="legacy-jsonl/1.0")
    return root


def _stop_terms(path: Path | None) -> set[str]:
    if path is None or not path.is_file():
        return set()
    return {
        line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "identify-topics":
        documents = list(load_documents_jsonl(
            args.documents, processing_version=args.processing_version
        ))
        identifier = AutoTopicIdentifier(
            DiscoveryTopicIdentifier(
                LegacyBertopicDiscoveryBackend(args.bertopic_config),
                small_corpus_backend=SmallCorpusTopicDiscoveryBackend(),
            ),
            mode=args.mode,
        )
        candidates = identifier.identify(documents, args.topic_table)
        count = write_topic_candidates_jsonl(args.output, candidates)
        modes = sorted({candidate.resolution.value for candidate in candidates})
        print(json.dumps({
            "documents": len(documents),
            "candidates": count,
            "resolutions": modes,
            "output": str(args.output),
            "status": "candidates_only",
        }, ensure_ascii=False))
        return 0
    extractors = [LegacyStatisticalTermExtractor.from_config(args.config, stop_terms=_stop_terms(args.stop_terms))]
    if args.knowledge_csv:
        extractors.append(KnowledgeSeedTermExtractor.from_csv(args.knowledge_csv))
    if bool(args.llm_results) != bool(args.sentences):
        raise ValueError("--llm-results and --sentences must be supplied together")
    if args.llm_results and args.sentences:
        extractors.append(LegacyLlmConsensusTermExtractor.from_legacy_jsonl(
            args.llm_results, args.sentences, minimum_votes=args.llm_minimum_votes
        ))
    extractor = CompositeTermExtractor(extractors)
    rows = []
    document_count = 0
    for document in load_documents_jsonl(args.documents, processing_version=args.processing_version):
        document_count += 1
        rows.extend(extractor.extract(document))
    count = write_term_candidates_jsonl(args.output, rows)
    print(json.dumps({
        "documents": document_count,
        "candidates": count,
        "extractors": [item.name for item in extractors],
        "output": str(args.output),
        "status": "candidates_only",
    }, ensure_ascii=False))
    return 0
