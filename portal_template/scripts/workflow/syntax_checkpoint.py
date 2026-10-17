"""Persist each syntax response before proceeding; final corpus is replaced only on success."""
from __future__ import annotations
import csv
import hashlib
import json
import os
import time
from .atomic_files import replace_with_retry, write_progress_json
from pathlib import Path
from typing import Any


def _append(path: Path, row: dict) -> None:
    # A killed write can leave a partial last line; isolate it from subsequent records.
    with path.open("ab+") as handle:
        handle.seek(0, 2)
        if handle.tell():
            handle.seek(-1, 2)
            if handle.read(1) != b"\n":
                handle.write(b"\n")
        handle.write((json.dumps(row, ensure_ascii=False)+"\n").encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


def _load_cache(path: Path) -> tuple[dict, int]:
    cache, invalid = {}, 0
    if path.exists():
        with path.open("rb") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row=json.loads(line.decode("utf-8-sig"))
                    if not isinstance(row,dict):
                        raise ValueError("not an object")
                    if row.get("cache_key") and row.get("tree"):
                        cache[row["cache_key"]]={"tree":row["tree"]}
                except (ValueError,TypeError,UnicodeError):
                    invalid += 1
    return cache, invalid


def build_syntax_tree_index(ctx):
    from . import stages as stage
    output=ctx.configured_path("index_output")
    output.mkdir(parents=True,exist_ok=True)
    items_path=output/"index_items.jsonl"
    total=sum(1 for _ in stage.read_jsonl(items_path))
    config=ctx.config.get("syntax_tree",{})
    encoding=str(ctx.config["index"].get("encoding","gb18030")).lower()
    if encoding!="gb18030":
        raise RuntimeError("句法结构语料编码必须配置为 gb18030，供 LangSC BCC 建索引")
    syntax_path=output/ctx.config["index"].get("syntax_tree_file_name","corpus_index_syntax_tree.txt")
    tree_partial=syntax_path.with_suffix(syntax_path.suffix+".partial")
    token_path=output/"index_item_tokens.jsonl"
    token_partial=token_path.with_suffix(".jsonl.partial")
    responses=output/"syntax_tree_responses.jsonl"
    cache_path=output/"syntax_tree_cache.jsonl"
    error_path=output/"syntax_tree_errors.csv"
    progress_path=output/"syntax_tree_progress.json"
    cache,invalid= _load_cache(cache_path)
    terms,_=stage._active_terms(ctx)
    tokenizer=stage._create_jieba_tokenizer(terms,ctx.resolve(ctx.config["linguistic"]["jieba_user_dict"]))
    base=os.environ.get("CORPUS_SYNTAX_TREE_BASE_URL",str(config.get("base_url","http://202.112.194.64:5000"))).rstrip("/")
    endpoint=str(config.get("endpoint","/struct_by_tokens"))
    url=base+(endpoint if endpoint.startswith("/") else "/"+endpoint)
    timeout=float(config.get("timeout_seconds",30))
    attempts=int(config.get("max_attempts",3))
    max_tokens=int(config.get("max_tokens_per_request",100))
    failure_limit=max(1,int(config.get("max_consecutive_failures",5)))
    errors=[]
    hits=successes=completed=consecutive=0
    item_id=""
    next_progress_retry=0.0
    progress_warning=False

    def progress(detail, status="running"):
        nonlocal next_progress_retry, progress_warning
        row={"status":status,"total":total,"completed":completed,"successes":successes,
             "failed_chunks":len(errors),"cache_hits":hits,"item_id":item_id,
             "detail":detail,"updated_at":stage.now_iso(),"invalid_cache_records":invalid}
        # A progress snapshot is advisory. A transient reader lock must not discard
        # completed work or turn a healthy remote response into a failed task.
        if status!="running" or time.monotonic()>=next_progress_retry:
            try:
                write_progress_json(progress_path,row)
                next_progress_retry=0.0
                progress_warning=False
            except PermissionError as exc:
                next_progress_retry=time.monotonic()+5.0
                if not progress_warning:
                    stage._emit_progress(3+round(completed/max(total,1)*82),
                        f"进度文件暂时无法替换，将自动重试；加工继续，回复与缓存仍逐条保存：{exc}")
                    progress_warning=True
        stage._emit_progress(3+round(completed/max(total,1)*82),detail)

    progress(f"准备句法树处理：0/{total} 条；可复用缓存 {len(cache)} 条；损坏缓存记录 {invalid}")
    fields=["item_id","doc_id","chunk_index","error","original_text"]
    error_header=not error_path.exists() or not error_path.stat().st_size
    try:
        with tree_partial.open("w",encoding=encoding,newline="\n") as tree_file, token_partial.open("w",encoding="utf-8") as tokens, error_path.open("a",encoding="utf-8-sig",newline="") as error_file:
            writer=csv.DictWriter(error_file,fieldnames=fields)
            if error_header:
                writer.writeheader();error_file.flush()
            current_doc=""
            for item in stage.read_jsonl(items_path):
                item_id=str(item["item_id"])
                words,pos=stage._segment_text_with_pos(str(item.get("text","")),tokenizer)
                chunks=stage._syntax_chunks(words,pos,max_tokens)
                token={"item_id":item_id,"doc_id":item.get("doc_id",""),"text":item.get("text",""),"words":words,"pos":pos,"syntax_chunk_count":len(chunks)}
                tokens.write(json.dumps(token,ensure_ascii=False)+"\n");tokens.flush()
                trees=[];failed=False
                for chunk_index,(chunk_words,chunk_pos) in enumerate(chunks):
                    body={"words":chunk_words,"pos":chunk_pos}
                    key=hashlib.sha256(json.dumps(dict(body,endpoint=url,schema=2),ensure_ascii=False,sort_keys=True).encode("utf-8")).hexdigest()
                    legacy=hashlib.sha256(json.dumps(body,ensure_ascii=False,sort_keys=True).encode("utf-8")).hexdigest()
                    # Old cache keys had no endpoint; accept them only for the original service.
                    cached=cache.get(key) or (cache.get(legacy) if url=="http://202.112.194.64:5000/struct_by_tokens" else None)
                    context=f"句法树 {completed}/{total} 条；当前 {item_id} 片段 {chunk_index+1}/{len(chunks)}；成功 {successes}；失败片段 {len(errors)}；复用 {hits}"
                    record={"item_id":item_id,"chunk_index":chunk_index,"chunk_count":len(chunks),"endpoint":url,"cache_key":key,"created_at":stage.now_iso()}
                    try:
                        if cached:
                            tree=str(cached["tree"]);payload={};source="cache";hits+=1
                            progress(context+"；复用已保存结果")
                        else:
                            tree,payload=stage._request_syntax_tree(url,chunk_words,chunk_pos,timeout,attempts,
                                on_attempt=lambda attempt:progress(context+f"；请求第 {attempt}/{max(attempts,1)} 次"))
                            _append(cache_path,{"cache_key":key,"tree":tree,"response":payload,"endpoint":url,"created_at":stage.now_iso()})
                            cache[key]={"tree":tree};source="api"
                        _append(responses,dict(record,status="success",source=source,words=chunk_words,pos=chunk_pos,response=payload,tree=tree))
                        trees.append(tree);consecutive=0
                    except (KeyboardInterrupt,SystemExit):
                        raise
                    except Exception as exc:
                        # Disk failures must stop immediately; do not pretend they were service failures.
                        if isinstance(exc,OSError) and not isinstance(exc,TimeoutError):
                            raise
                        failed=True;consecutive+=1
                        error={"item_id":item_id,"doc_id":str(item.get("doc_id","")),"chunk_index":str(chunk_index),"error":f"{type(exc).__name__}: {exc}","original_text":str(item.get("text",""))}
                        writer.writerow(error);error_file.flush();os.fsync(error_file.fileno());errors.append(error)
                        _append(responses,dict(record,status="error",source="api",words=chunk_words,pos=chunk_pos,error=str(exc)))
                        progress(context+f"；失败已保存：{exc}")
                        if consecutive>=failure_limit:
                            raise RuntimeError(f"句法服务连续失败 {consecutive} 个片段，已停止本步骤，避免长时间空转；已保存回复和缓存。检查服务后重新运行句法步骤，成功片段自动复用，失败片段重新请求。最近错误：{exc}") from exc
                if not failed and trees:
                    tree=trees[0] if len(trees)==1 else "(DIALOGUE "+" ".join(f"(UNIT_{i+1:04d} {value})" for i,value in enumerate(trees))+")"
                    successes+=1
                else:
                    tree=str(item.get("text",""))
                if item["doc_id"]!=current_doc:
                    tree_file.write(f"Doc {item['doc_name']}\n");current_doc=str(item["doc_id"])
                tree_file.write("#Global "+";".join(item["global_pairs"])+"\n")
                tree_file.write("#Group"+(" "+";".join(item["group_pairs"]) if item["group_pairs"] else "")+"\n")
                tree_file.write(f"Item: {stage._bcc_safe_tree(tree)}\n");tree_file.flush()
                completed+=1
                progress(f"句法树处理：{completed}/{total} 条；成功 {successes}；失败片段 {len(errors)}；复用 {hits}")
        replace_with_retry(tree_partial,syntax_path);replace_with_retry(token_partial,token_path)
    except BaseException as exc:
        progress(f"句法步骤已中止：{exc}；回复和缓存已保存，可重新运行句法步骤续跑",status="interrupted" if isinstance(exc,(KeyboardInterrupt,SystemExit)) else "failed")
        raise
    progress(f"句法步骤完成：{completed}/{total} 条；成功 {successes}；失败片段 {len(errors)}；复用 {hits}",status="completed_with_errors" if errors else "completed")
    return syntax_path,{"syntax_items":total,"syntax_successes":successes,"syntax_failures":len(errors),"syntax_cache_hits":hits},errors
