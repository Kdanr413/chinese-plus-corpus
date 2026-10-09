"""Command-line access to the Workspace and Corpus Release lifecycle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .manager import (
    create_release,
    create_workspace,
    create_workspace_from_release,
    read_release,
    validate_release,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m corpus_releases")
    subparsers = parser.add_subparsers(dest="command", required=True)

    workspace = subparsers.add_parser("workspace-create", help="create an editable workspace")
    workspace.add_argument("--workspaces-root", type=Path, default=Path("data/workspaces"))
    workspace.add_argument("--corpus-id", required=True)
    workspace.add_argument("--workspace-id", required=True)

    release = subparsers.add_parser("release-create", help="freeze a workspace as a release")
    release.add_argument("--workspace", type=Path, required=True)
    release.add_argument("--releases-root", type=Path, default=Path("data/releases"))
    release.add_argument("--corpus-id", required=True)
    release.add_argument("--version", required=True)
    release.add_argument("--corpus-format-version", required=True)
    release.add_argument("--pipeline-version", required=True)
    release.add_argument("--search-engine-version", required=True)
    release.add_argument("--based-on-version")

    verify = subparsers.add_parser("release-verify", help="verify every release asset")
    verify.add_argument("release", type=Path)

    show = subparsers.add_parser("release-show", help="read a verified release manifest")
    show.add_argument("--releases-root", type=Path, default=Path("data/releases"))
    show.add_argument("--corpus-id", required=True)
    show.add_argument("--version", required=True)
    show.add_argument("--no-verify", action="store_true")

    reprocess = subparsers.add_parser("workspace-from-release", help="start reprocessing an old release")
    reprocess.add_argument("--releases-root", type=Path, default=Path("data/releases"))
    reprocess.add_argument("--workspaces-root", type=Path, default=Path("data/workspaces"))
    reprocess.add_argument("--corpus-id", required=True)
    reprocess.add_argument("--version", required=True)
    reprocess.add_argument("--workspace-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "workspace-create":
        result = create_workspace(args.workspaces_root, args.corpus_id, args.workspace_id)
        output = {"workspace": str(result.path), "processing_task": str(result.processing_task_path)}
    elif args.command == "release-create":
        based_on = (
            {"corpus_id": args.corpus_id, "version": args.based_on_version}
            if args.based_on_version
            else None
        )
        result = create_release(
            args.workspace,
            args.releases_root,
            corpus_id=args.corpus_id,
            version=args.version,
            corpus_format_version=args.corpus_format_version,
            pipeline_version=args.pipeline_version,
            search_engine_version=args.search_engine_version,
            based_on_release=based_on,
        )
        output = {"release": str(result.path), "manifest": result.manifest}
    elif args.command == "release-verify":
        report = validate_release(args.release)
        output = report.as_dict()
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0 if report.valid else 1
    elif args.command == "release-show":
        result = read_release(
            args.releases_root,
            args.corpus_id,
            args.version,
            verify=not args.no_verify,
        )
        output = {"release": str(result.path), "manifest": result.manifest}
    else:
        result = create_workspace_from_release(
            args.releases_root,
            args.workspaces_root,
            corpus_id=args.corpus_id,
            version=args.version,
            workspace_id=args.workspace_id,
        )
        output = {
            "workspace": str(result.path),
            "base_release": result.base_release,
            "processing_task": str(result.processing_task_path),
        }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0
