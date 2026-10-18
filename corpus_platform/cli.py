"""Beginner-facing command line for the complete corpus platform workflow."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from corpus_pipeline.service import SUPPORTED_STAGES

from .service import PlatformCoordinator, PlatformStatus


def _configure_console() -> None:
    """Keep Chinese guidance readable in Windows terminals and redirected logs."""
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m corpus_platform",
        description="“中文+”语料库全流程统一入口",
    )
    parser.add_argument("--project-root", type=Path, help="迁移后项目根目录；通常无需填写")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="检查环境、依赖、配置和数据库")
    doctor.add_argument("--offline", action="store_true", help="不连接 PostgreSQL")
    doctor.add_argument("--json", action="store_true")

    status = sub.add_parser("status", help="显示全流程目前走到哪一步")
    status.add_argument("--offline", action="store_true", help="不连接 PostgreSQL")
    status.add_argument("--json", action="store_true")

    process = sub.add_parser("process", help="调用现有 corpus_pipeline 加工能力")
    process.add_argument("stage", choices=SUPPORTED_STAGES)
    process.add_argument("--dry-run", action="store_true", help="只显示计划，不加工文件")
    process.add_argument("--no-rebuild", action="store_true")

    publish = sub.add_parser("publish", help="结构化语料 → Workspace → BCC 索引 → Release → Registry")
    publish.add_argument("--version", required=True)
    publish.add_argument("--source", type=Path, help="句法树结构化语料；默认读取 Pipeline 当前版本")
    publish.add_argument("--catalog", type=Path, help="可选 index_items.jsonl")
    publish.add_argument("--workspace-id")
    publish.add_argument("--parent-version")
    publish.add_argument("--no-register", action="store_true", help="只创建文件系统 Release")
    publish.add_argument("--make-default", action="store_true", help="登记后设为默认检索版本")
    publish.add_argument("--dry-run", action="store_true", help="必须先用此项核对目标；不会写文件或数据库")

    verify = sub.add_parser("verify", help="同时校验 Release 文件与 Registry 登记")
    verify.add_argument("--corpus-id", default="chinese-plus")
    verify.add_argument("--version", required=True)
    verify.add_argument("--offline", action="store_true", help="只检查文件系统 Release")

    serve = sub.add_parser("serve", help="启动统一登录、独立构建和分享平台")
    serve.add_argument("--legacy", action="store_true", help="本机管理员兼容模式：直接打开原单用户工作台")
    serve.add_argument("--setup", action="store_true", help="初始化账户表，首次使用在终端创建账号")
    public = sub.add_parser("public-site", help="公开介绍、只读检索与构建程序下载（独立入口）")
    public.add_argument("--config",type=Path,default=Path("configs/public_site.example.json"))
    public.add_argument("--host",default="127.0.0.1")
    public.add_argument("--port",type=int,default=8002)
    portal = sub.add_parser("portal", help="多用户独立项目与只读分享")
    portal.add_argument("portal_args", nargs=argparse.REMAINDER)
    return parser


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _print_status(value: PlatformStatus) -> None:
    labels = {"ready": "已就绪", "partial": "进行中", "waiting": "待完成", "available": "可启动", "blocked": "未就绪"}
    print(f"项目：{value.project_root}\n")
    for phase in value.phases:
        print(f"{phase.number}. {phase.name} [{labels.get(phase.state, phase.state)}]")
        print(f"   {phase.details}")
    print(f"\n下一步：{value.next_action}")
    print(f"命令：{value.next_command}")


def main(argv: Sequence[str] | None = None) -> int:
    _configure_console()
    args = _parser().parse_args(argv)
    try:
        if args.command == "public-site":
            from corpus_delivery.public_site import main as public_main
            options=["--config",str(args.config),"--host",args.host,"--port",str(args.port)]
            if args.project_root:options=["--project-root",str(args.project_root),*options]
            return public_main(options)
        if args.command == "portal":
            from corpus_portal.cli import main as portal_main
            return portal_main(args.portal_args)
        if args.command == "serve" and not args.legacy:
            from dotenv import load_dotenv
            from .paths import resolve_project_root
            load_dotenv(resolve_project_root(args.project_root) / ".env", override=False)
            from corpus_api.__main__ import _port
            from corpus_portal.cli import main as portal_main
            options = ["serve", "--host", os.getenv("API_HOST", "127.0.0.1"), "--port", str(_port())]
            if args.setup: options.append("--setup")
            return portal_main(options)
        coordinator = PlatformCoordinator(args.project_root)
        if args.command == "doctor":
            result = coordinator.doctor(check_database=not args.offline)
            _print_json(result)
            return 0 if result["ok"] else 1
        if args.command == "status":
            result = coordinator.status(check_database=not args.offline)
            _print_json(result.as_dict()) if args.json else _print_status(result)
            return 0
        if args.command == "process":
            result = coordinator.process(args.stage, dry_run=args.dry_run, rebuild=not args.no_rebuild)
            if isinstance(result, dict):
                _print_json(result)
            else:
                _print_json(result.as_dict())
            return 0
        if args.command == "publish":
            options = {
                "version": args.version,
                "source": args.source,
                "workspace_id": args.workspace_id,
                "parent_version": args.parent_version,
                "register": not args.no_register,
                "make_default": args.make_default,
            }
            if args.dry_run:
                _print_json(coordinator.publish_plan(**options))
            else:
                _print_json(coordinator.publish(catalog=args.catalog, **options))
            return 0
        if args.command == "verify":
            result = coordinator.verify(args.corpus_id, args.version, check_database=not args.offline)
            _print_json(result)
            return 0 if result["ok"] else 1
        from corpus_api.__main__ import main as serve
        serve()
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        return 1
