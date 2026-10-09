"""Command-line adapter for :mod:`corpus_pipeline`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .errors import PipelineError, PipelineStageError
from .models import PipelineOptions
from .service import CorpusPipeline, SUPPORTED_STAGES


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="专门语料库治理、加工、验证、反馈与发布入口")
    parser.add_argument("stage", choices=[*SUPPORTED_STAGES, "doctor"])
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--project-root", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--collect", action="store_true", help="在采集阶段实际执行网络采集；默认只盘点")
    parser.add_argument("--no-rebuild", action="store_true", help="复用已有清洗/话题产物")
    parser.add_argument("--continue-on-failure", action="store_true", help="仅用于诊断；验收失败后仍继续")
    parser.add_argument("--limit", type=int, default=100, help="term-llm 单次最多执行的模型请求数")
    parser.add_argument("--defer-topic-llm", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        pipeline = CorpusPipeline(
            config_path=args.config,
            project_root=args.project_root,
            event_handler=print,
        )
        if args.stage == "doctor":
            checks = pipeline.diagnose()
            for check in checks:
                state = "OK" if check.passed else "MISSING"
                value = check.path if check.path is not None else check.details
                print(f"{state:7} {check.label}: {value}")
            return 0 if all(check.passed for check in checks) else 1
        if args.dry_run:
            for code, description in pipeline.plan(args.stage):
                print(f"{code}: {description}")
            return 0
        options = PipelineOptions(
            rebuild=not args.no_rebuild,
            run_collection=args.collect,
            continue_on_failure=args.continue_on_failure,
            term_limit=args.limit,
            defer_topic_llm=args.defer_topic_llm,
        )
        execution = pipeline.run(args.stage, options)
        return 0 if execution.status != "failed" else 2
    except PipelineStageError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (PipelineError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

