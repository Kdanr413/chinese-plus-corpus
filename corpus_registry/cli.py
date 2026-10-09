from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from .migrations import initialize_database
from .repository import CorpusRegistry


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m corpus_registry")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="apply pending PostgreSQL migrations")

    corpus = sub.add_parser("corpus-create")
    corpus.add_argument("--corpus-id", required=True)
    corpus.add_argument("--name", required=True)
    corpus.add_argument("--description", default="")

    register = sub.add_parser("release-register")
    register.add_argument("--corpus-id", required=True)
    register.add_argument("--version", required=True)
    register.add_argument("--release-path", type=Path, required=True)
    register.add_argument("--corpus-format-version", required=True)
    register.add_argument("--pipeline-version", required=True)
    register.add_argument("--search-engine-version", required=True)
    register.add_argument("--status", default="ready")
    register.add_argument("--searchable", action="store_true")
    register.add_argument("--parent-version")
    register.add_argument("--make-default", action="store_true")

    show = sub.add_parser("release-show")
    show.add_argument("--corpus-id", required=True)
    show.add_argument("--version")

    verify = sub.add_parser("release-verify-files")
    verify.add_argument("--corpus-id", required=True)
    verify.add_argument("--version", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "init-db":
        print(_json({"applied_migrations": initialize_database()}))
        return 0
    registry = CorpusRegistry()
    if args.command == "corpus-create":
        output = registry.create_corpus(args.corpus_id, args.name, args.description)
    elif args.command == "release-register":
        output = registry.register_release(
            corpus_key=args.corpus_id,
            version=args.version,
            release_path=args.release_path,
            corpus_format_version=args.corpus_format_version,
            pipeline_version=args.pipeline_version,
            search_engine_version=args.search_engine_version,
            status=args.status,
            searchable=args.searchable,
            parent_version=args.parent_version,
            make_default=args.make_default,
        )
    elif args.command == "release-show":
        version = registry.get_version(args.corpus_id, args.version)
        output = {"version": version, "files": registry.list_files(args.corpus_id, version["version"])}
    else:
        output = registry.verify_registered_files(args.corpus_id, args.version)
    print(_json(output))
    return 0
