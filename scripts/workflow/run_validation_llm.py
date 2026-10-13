#!/usr/bin/env python3
"""Run auditable OpenAI-compatible validation tasks; raw replies never overwrite machine evidence."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
import shutil
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(PROJECT_ROOT))

from scripts.workflow.prompting import build_messages, chat_completions_url, load_prompt_config, parse_json_content, prompt_key_for_task
from scripts.workflow.model_cache import AnnotationCache


def main() -> int:
    parser = argparse.ArgumentParser(description="运行话题/术语验证任务并保存原始回复与解析结果")
    parser.add_argument("--mode", choices=["cleaning", "topics", "terms"], required=True)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--responses", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=100, help="本次最多执行的任务数；0 表示处理所有未完成任务")
    parser.add_argument("--require-complete", action="store_true", help="任一任务失败或仍有未执行任务时返回失败")
    parser.add_argument("--reset", action="store_true", help="开始前清空该类任务的旧回复记录")
    parser.add_argument("--max-attempts", type=int, default=3, help="单条任务遇到空回复、解析或临时接口错误时的最多尝试次数")
    parser.add_argument("--prompt-config", type=Path, default=PROJECT_ROOT / "configs" / "llm" / "prompts.json")
    parser.add_argument('--cache-root',type=Path,default=PROJECT_ROOT/'data/model_annotations')
    args = parser.parse_args()
    if args.limit < 0:
        parser.error('--limit 不能为负数；0 表示不限制任务数')
    prefix = "CLEANING" if args.mode == "cleaning" else ("TOPIC" if args.mode == "topics" else "TERM")
    api_key = os.environ.get(f"{prefix}_LLM_API_KEY", "")
    model = os.environ.get(f"{prefix}_LLM_MODEL", "")
    base_url = os.environ.get(f"{prefix}_LLM_BASE_URL", "https://api.openai.com/v1")
    if not api_key or not model:
        raise RuntimeError(f"缺少 {prefix}_LLM_API_KEY 或 {prefix}_LLM_MODEL")
    tasks = [json.loads(line) for line in args.tasks.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    prompt_config = load_prompt_config(args.prompt_config)
    cache=AnnotationCache(args.cache_root)
    if args.reset and args.responses.exists():
        archive=args.cache_root/'runs'/datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        archive.mkdir(parents=True,exist_ok=False)
        shutil.copy2(args.responses,archive/(args.mode+'-responses.jsonl'))
        args.responses.unlink()
    completed=set()
    # A row index alone is not identity: a new round may reorder the corpus.
    for line in (args.responses.read_text(encoding='utf-8-sig').splitlines() if args.responses.exists() else []):
        if not line.strip():continue
        try:
            row=json.loads(line);index=int(row.get('task_index',-1))
            if row.get('status')!='success' or not row.get('cache_key') or not 0<=index<len(tasks):continue
            task=tasks[index];prompt_key=prompt_key_for_task(task,args.mode)
            key,_=cache.identify(base_url,model,build_messages(prompt_config['prompts'][prompt_key],task))
            if row['cache_key']==key:completed.add(str(index))
        except (ValueError,TypeError):continue
    args.responses.parent.mkdir(parents=True, exist_ok=True)
    executed = 0
    failed = 0
    reused = 0
    with args.responses.open("a", encoding="utf-8") as output:
        for index, task in enumerate(tasks):
            if str(index) in completed or (args.limit > 0 and executed >= args.limit):
                continue
            prompt_key = prompt_key_for_task(task, args.mode)
            messages = build_messages(prompt_config["prompts"][prompt_key], task)
            cache_key,bindings=cache.identify(base_url,model,messages)
            body = json.dumps({"model": model, "temperature": 0.1, "messages": messages}, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(chat_completions_url(base_url), data=body, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
            record = {"task_index": index, "mode": args.mode, "prompt_key": prompt_key, "prompt_updated_at": prompt_config.get("updated_at", ""), "model": model, "received_at": datetime.now().astimezone().isoformat(timespec="seconds")}
            attempt_errors = []
            cached=cache.get(cache_key,bindings)
            for attempt in range(1, max(1, args.max_attempts) + 1):
                response_body = ""
                content = ""
                try:
                    if cached is not None:
                        response_body=json.dumps({'choices':[{'message':{'content':cached}}]})
                    else:
                        with urllib.request.urlopen(request, timeout=180) as response:
                            response_body = response.read().decode("utf-8", errors="replace")
                    if not response_body.strip():
                        raise ValueError("模型接口返回空响应体")
                    payload = json.loads(response_body)
                    content_value = payload.get("choices", [{}])[0].get("message", {}).get("content")
                    content = "" if content_value is None else str(content_value)
                    if not content.strip():
                        raise ValueError("模型返回的 message.content 为空")
                    parsed = parse_json_content(content)
                    record.update({"status": "success", "attempt_count": attempt, "raw_content": content, "parsed": parsed, "response_payload": payload, "failed_attempts": attempt_errors,'cache_key':cache_key,'cache_reused':cached is not None})
                    if cached is None:cache.put(cache_key,bindings,content)
                    else:reused+=1
                    break
                except urllib.error.HTTPError as exc:
                    response_body = exc.read().decode("utf-8", errors="replace")
                    error_type = f"HTTP_{exc.code}"
                    error_text = f"HTTP {exc.code}: {response_body or exc.reason}"
                except Exception as exc:
                    error_type = type(exc).__name__
                    error_text = str(exc)
                attempt_errors.append({"attempt": attempt, "received_at": datetime.now().astimezone().isoformat(timespec="seconds"), "error_type": error_type, "error": error_text, "raw_response_body": response_body, "raw_content": content})
                if attempt < max(1, args.max_attempts):
                    time.sleep(min(1.5 * attempt, 5.0))
            if record.get("status") != "success":
                last = attempt_errors[-1] if attempt_errors else {"error_type": "unknown", "error": "未知错误"}
                record.update({"status": "error", "attempt_count": len(attempt_errors), "error_type": last["error_type"], "error": last["error"], "failed_attempts": attempt_errors})
                failed += 1
            output.write(json.dumps(record, ensure_ascii=False) + "\n"); output.flush(); executed += 1
            print(f'CORPUS_PROGRESS\t{min(100, int((len(completed)+executed)/max(1,len(tasks))*100))}\t模型复核 {len(completed)+executed}/{len(tasks)}；复用 {reused} 条',flush=True)
            if record.get("status") == "error":
                print(json.dumps({
                    "task_index": index,
                    "status": "error",
                    "attempt_count": record.get("attempt_count", 0),
                    "error_type": record.get("error_type", "unknown"),
                    "error": record.get("error", "未知错误"),
                    "response_record": str(args.responses),
                }, ensure_ascii=False), flush=True)
    remaining = max(len(tasks) - len(completed) - executed, 0)
    print(json.dumps({"mode": args.mode, "executed": executed, "failed": failed, "remaining": remaining,'cache_reused':reused}, ensure_ascii=False))
    return 2 if args.require_complete and (failed or remaining) else 0


if __name__ == "__main__": raise SystemExit(main())
