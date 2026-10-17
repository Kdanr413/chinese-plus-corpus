from __future__ import annotations
import argparse
import getpass
import json
import shutil
import os
import sys
import threading
import time
import urllib.request
import webbrowser
from dataclasses import replace
import uuid
from pathlib import Path
from dotenv import load_dotenv
from .workspaces import ROOT, PortalSettings, initialize_workspace
from .store import PortalStore

def setup_accounts(store):
    store.initialize()
    if store.has_users():
        print('账号已就绪，请在网页登录。')
        return
    if not sys.stdin.isatty():
        raise ValueError('首次账号尚未建立，请在终端执行 python -m corpus_platform portal setup，或双击启动语料库平台.cmd。')
    print('首次使用：在此创建你的登录账号。原语料和历史结果不会移动。')
    username=input('账号（3至64位英文字母、数字或 _-.）：').strip()
    password=getpass.getpass('密码（至少3字符，不会显示）：')
    confirm=getpass.getpass('再次输入密码：')
    if password!=confirm: raise ValueError('两次密码不一致，请重新启动并创建账号')
    store.add_user(username,password)
    print('账号已建立，可以登录并创建自己的语料库项目。')

def main(argv=None):
    load_dotenv(ROOT/'.env',override=False)
    p=argparse.ArgumentParser(description='独立用户项目与语料分享平台')
    commands=p.add_subparsers(dest='command',required=True)
    commands.add_parser('init-db')
    commands.add_parser('setup')
    prepare=commands.add_parser('prepare-template')
    prepare.add_argument('--source',type=Path,required=True)
    imp=commands.add_parser('import-release')
    imp.add_argument('--project',type=uuid.UUID,required=True)
    imp.add_argument('--source',type=Path,required=True)
    add=commands.add_parser('add-user')
    add.add_argument('username')
    reset=commands.add_parser('reset-password')
    reset.add_argument('username')
    serve=commands.add_parser('serve')
    serve.add_argument('--host',default=os.getenv('API_HOST','127.0.0.1'))
    serve.add_argument('--port',type=int,default=int(os.getenv('API_PORT','8000')))
    serve.add_argument('--setup',action='store_true')
    a=p.parse_args(argv)
    settings=PortalSettings.environment()
    if a.command=='serve':
        settings=replace(settings,bind_host=a.host)
        if not 1<=a.port<=65535: p.error('端口必须在 1 至 65535 之间')
        if not os.getenv('PORTAL_PUBLIC_URL'):
            settings=replace(settings,public_url=f'http://127.0.0.1:{a.port}')
    store=PortalStore()
    if a.command=='setup':
        setup_accounts(store)
        return 0
    if a.command=='prepare-template':
        source=a.source.resolve()
        if not (source/'configs/pipeline.json').is_file(): p.error('源目录不是构建程序包')
        if settings.template.exists(): p.error('模板已存在，拒绝覆盖')
        # Only a clean distribution may be used as a template.
        if any(x.is_file() and x.name!='.gitkeep' for x in (source/'data').rglob('*')):
            p.error('源目录含语料或历史结果，请使用干净的构建发行目录')
        if (source/'.env').exists(): p.error('源目录含实际 .env，不能作为共享模板')
        shutil.copytree(source,settings.template,ignore=shutil.ignore_patterns('.git','runtime','runtime-win64.zip','__pycache__','*.pyc','.env'))
        print('干净项目模板已准备')
        return 0
    if a.command=='import-release':
        from corpus_registry.db import connect
        from .imports import import_release
        with connect() as c:
            row=c.execute('SELECT id FROM corpus_portal.projects WHERE id=%s',(a.project,)).fetchone()
        if row is None: p.error('目标项目不存在；请先由该用户在网页创建项目')
        import_release(settings,a.project,a.source)
        print('正式版本已复制并校验；原版本未移动、未覆盖。重开项目构建服务后可检索。')
        return 0
    if a.command=='init-db':
        store.initialize()
        print('账户、项目与分享表已初始化；没有改写原语料表')
        return 0
    if a.command in {'add-user','reset-password'}:
        password=getpass.getpass('新密码（至少3字符，不会显示）：')
        confirm=getpass.getpass('再次输入密码：')
        if password!=confirm: p.error('两次密码不一致')
        if a.command=='add-user': store.add_user(a.username,password)
        else:
            from corpus_registry.db import connect
            from .security import password_hash
            with connect() as c:
                row=c.execute('UPDATE corpus_portal.users SET password_hash=%s WHERE username=%s RETURNING id',(password_hash(password),a.username.strip().lower())).fetchone()
                if not row: p.error('账号不存在')
                c.execute('DELETE FROM corpus_portal.sessions WHERE user_id=%s',(row['id'],))
        print('账号已更新')
        return 0
    if a.setup: setup_accounts(store)
    else: store.initialize()
    print(f'统一构建平台：{settings.public_url}/')
    if not store.has_users(): print('还没有账号；可双击主启动器创建账号，或运行 python -m corpus_platform portal setup。')
    if os.getenv('API_OPEN_BROWSER','0')=='1':
        def open_when_ready():
            for _ in range(120):
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{a.port}/health',timeout=.5):
                        webbrowser.open(settings.public_url+'/')
                        return
                except OSError: time.sleep(.25)
        threading.Thread(target=open_when_ready,daemon=True).start()
    from .app import create_app
    import uvicorn
    uvicorn.run(create_app(settings,store),host=a.host,port=a.port,workers=1,access_log=False,proxy_headers=False)
    return 0

if __name__=='__main__': raise SystemExit(main())
