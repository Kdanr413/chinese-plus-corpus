from __future__ import annotations
import json
import re
import shutil
import uuid
from pathlib import Path
from corpus_releases import validate_release

def import_release(settings, project_id, source):
    source=Path(source).resolve()
    report=validate_release(source)
    if not report.valid: raise ValueError('源版本校验失败，拒绝导入')
    manifest=json.loads((source/'manifest.json').read_text(encoding='utf-8-sig'))
    key,version=manifest['corpus_id'],manifest['version']
    if not all(re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',x) and x not in {'.','..'} for x in [key,version]):
        raise ValueError('版本标识无效')
    project=settings.project_root(project_id)
    if not (project/'configs/pipeline.json').is_file(): raise ValueError('目标项目工作区不存在')
    target=(project/'data/releases'/key/version).resolve()
    if not target.is_relative_to(project): raise ValueError('目标路径无效')
    if target.exists(): raise ValueError('目标版本已存在，拒绝覆盖')
    target.parent.mkdir(parents=True,exist_ok=True)
    staging=target.parent/('.import_'+uuid.uuid4().hex)
    shutil.copytree(source,staging)
    if not validate_release(staging).valid: raise ValueError('复制后校验失败，未发布临时副本')
    staging.rename(target)
    return target
