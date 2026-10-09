from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import json
import os
import re
import shutil
import uuid
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]

@dataclass
class PortalSettings:
    storage: Path
    template: Path
    public_url: str='http://127.0.0.1:8000'
    max_workers: int=4
    max_body: int=64*1024*1024
    project_quota: int=10*1024*1024*1024
    @classmethod
    def environment(cls):
        def path(key,default):
            p=Path(os.getenv(key,default))
            return (p if p.is_absolute() else ROOT/p).resolve()
        url=os.getenv('PORTAL_PUBLIC_URL',f"http://127.0.0.1:{os.getenv('API_PORT','8000')}").rstrip('/')
        parsed=urlsplit(url)
        if parsed.scheme not in {'http','https'} or not parsed.netloc or parsed.path:
            raise ValueError('PORTAL_PUBLIC_URL must be a site origin without a path')
        return cls(path('PORTAL_STORAGE','data/portal'),path('PORTAL_TEMPLATE','portal_template'),url,int(os.getenv('PORTAL_MAX_WORKERS','4')))
    def project_root(self, pid):
        pid=uuid.UUID(str(pid)).hex
        root=(self.storage/'projects'/pid).resolve()
        if not root.is_relative_to(self.storage.resolve()): raise ValueError('unsafe project path')
        return root
    @property
    def secure_cookie(self): return self.public_url.startswith('https://')

def initialize_workspace(settings, pid, name):
    source=settings.template.resolve()
    if not (source/'configs/pipeline.json').is_file():
        raise ValueError('请先运行 portal prepare-template，建立不含历史语料的模板')
    root=settings.project_root(pid)
    root.parent.mkdir(parents=True,exist_ok=True)
    if root.exists(): raise ValueError('项目目录已存在，拒绝覆盖')
    # The template has only clean program/configuration/input interfaces.
    shutil.copytree(source,root,ignore=shutil.ignore_patterns('.git','runtime','runtime-win64.zip','__pycache__','*.pyc','.env'))
    shutil.copy2(ROOT/'corpus_registry/db.py',root/'corpus_registry/db.py')
    for relative in ['data/raw/manual','data/raw/web','data/releases','data/workspaces','logs']:
        (root/relative).mkdir(parents=True,exist_ok=True)
    p=root/'configs/pipeline.json'
    config=json.loads(p.read_text(encoding='utf-8-sig'))
    config['project']['name']=name
    p.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
    return root

def verify_payload(payload, project_root, allowed_python):
    if not isinstance(payload,dict): return
    for key,value in payload.items():
        if key=='base_url' and value:
            url=urlsplit(str(value))
            hosts={x.strip().lower() for x in os.getenv('PORTAL_LLM_ALLOWED_HOSTS','api.openai.com').split(',') if x.strip()}
            if url.scheme!='https' or url.hostname not in hosts or url.username or url.password or url.port not in {None,443}:
                raise ValueError('模型接口必须使用管理员允许的 HTTPS 服务地址')
        elif key=='model_id' and value:
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}',str(value)):
                raise ValueError('模型标识只能包含英文字母、数字、下划线或连字符')
        elif key=='python_executable' and value:
            if str(Path(str(value)).resolve()) not in allowed_python:
                raise ValueError('运行环境由服务器管理，不能指定其他可执行程序')
        elif key in {'source_path','topic_table_path'} and value:
            path=Path(str(value))
            path=(path if path.is_absolute() else project_root/path).resolve()
            if not path.is_relative_to(project_root.resolve()):
                raise ValueError('不能读取其他项目或服务器目录中的语料')
        elif isinstance(value,dict): verify_payload(value,project_root,allowed_python)
        elif isinstance(value,list):
            for item in value:
                if isinstance(item,dict): verify_payload(item,project_root,allowed_python)
