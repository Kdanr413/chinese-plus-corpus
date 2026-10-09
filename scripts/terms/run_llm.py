#!/usr/bin/env python3
"""Execute term LLM tasks and retain raw replies plus parsed term records."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def parse_terms(content: str) -> list[str]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    value = json.loads(cleaned)
    if isinstance(value, dict):
        value = value.get("terms", [])
    if not isinstance(value, list):
        raise ValueError("model response must be a JSON array or an object with terms")
    return [str(item).strip() for item in value if str(item).strip()]


def request(base_url: str, api_key: str, model: str, prompt: str) -> tuple[dict[str, Any], str]:
    body = json.dumps({"model": model, "temperature": 0.1, "messages": [{"role": "user", "content": prompt}]}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=body, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"LLM HTTP {exc.code}: {detail}") from exc
    return payload, str(payload["choices"][0]["message"]["content"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行术语大模型任务并保存完整回复审计记录")
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--responses", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--model", default=os.environ.get("TERM_LLM_MODEL"))
    parser.add_argument("--base-url", default=os.environ.get("TERM_LLM_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--api-key-env", default="TERM_LLM_API_KEY")
    parser.add_argument("--limit", type=int, default=100)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_key = os.environ.get(args.api_key_env)
    if not api_key or not args.model:
        raise RuntimeError(f"请先设置 {args.api_key_env} 和 TERM_LLM_MODEL；模型调用不会在缺少显式配置时自动发生。")
    completed = {(str(row.get("sentence_id")), int(row.get("run_id", 0))) for row in read_jsonl(args.responses) if row.get("status") == "success"}
    executed = 0
    for task in read_jsonl(args.tasks):
        sentence_id = str(task.get("sentence_id", ""))
        for run_id in range(1, int(task.get("repetitions", 5)) + 1):
            if (sentence_id, run_id) in completed:
                continue
            if executed >= args.limit:
                print(json.dumps({"executed": executed, "limit": args.limit, "status": "partial"}, ensure_ascii=False))
                return 0
            timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
            try:
                payload, content = request(args.base_url, api_key, args.model, str(task.get("prompt", "")))
                terms = parse_terms(content)
                append_jsonl(args.responses, {"sentence_id": sentence_id, "run_id": run_id, "model": args.model, "received_at": timestamp, "status": "success", "raw_content": content, "response_payload": payload, "parsed_terms": terms})
                append_jsonl(args.results, {"sentence_id": sentence_id, "run_id": run_id, "model": args.model, "terms": terms, "response_status": "success"})
            except Exception as exc:
                append_jsonl(args.responses, {"sentence_id": sentence_id, "run_id": run_id, "model": args.model, "received_at": timestamp, "status": "error", "error": str(exc)})
            executed += 1
            time.sleep(0.2)
    print(json.dumps({"executed": executed, "status": "complete"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
