from __future__ import annotations
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from corpus_registry.db import connect
from .workspaces import ROOT

@dataclass
class Worker:
    process: object
    port: int
    token: str
    log: object

class WorkerSupervisor:
    def __init__(self,settings):
        self.settings=settings
        self.workers={}
        self.lock=threading.Lock()
    def ensure(self,pid):
        pid=uuid.UUID(str(pid))
        with self.lock:
            old=self.workers.get(pid)
            if old and old.process.poll() is None: return old
            if old: old.log.close()
            active=sum(w.process.poll() is None for w in self.workers.values())
            if active>=self.settings.max_workers: raise ValueError('服务器当前工作台已满，请联系管理员或稍后重试')
            root=self.settings.project_root(pid)
            if not (root/'configs/pipeline.json').is_file(): raise ValueError('项目工作区不存在')
            schema='p_'+pid.hex
            with connect() as c: c.execute('CREATE SCHEMA IF NOT EXISTS '+schema)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0))
                port=sock.getsockname()[1]
            token=secrets.token_urlsafe(32)
            env=dict(os.environ,CORPUS_DB_SCHEMA=schema,CORPUS_INTERNAL_TOKEN=token,CORPUS_PROJECT_ROOT=str(root),CORPUS_DATA_ROOT='data',ADMIN_API_TOKEN=token,PYTHONUTF8='1')
            # Keep generated Numba filenames below Windows' path limit, per project.
            env['CORPUS_NUMBA_CACHE_DIR']=str(ROOT/'.cache'/'numba'/pid.hex)
            env.pop('PYTHONPATH',None)
            for key in list(env):
                if key.startswith(('LLM_', 'TERM_LLM_', 'OPENAI_', 'ANTHROPIC_', 'AZURE_OPENAI_')):
                    env.pop(key,None)
            log_path=root/'logs/worker.log'
            log_path.parent.mkdir(parents=True,exist_ok=True)
            log=log_path.open('ab')
            options={'creationflags':subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP} if os.name=='nt' else {'start_new_session':True}
            process=subprocess.Popen([sys.executable,'-m','corpus_portal.worker','--project',str(root),'--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log,**options)
            worker=Worker(process,port,token,log)
            self.workers[pid]=worker
            for _ in range(120):
                if process.poll() is not None: break
                try:
                    request=urllib.request.Request(f'http://127.0.0.1:{port}/health',headers={'x-corpus-internal':token})
                    with urllib.request.urlopen(request,timeout=.5): return worker
                except OSError: time.sleep(.25)
            if process.poll() is None:
                if os.name=='nt': subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True)
                else:
                    import signal
                    os.killpg(process.pid,signal.SIGTERM)
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill()
            log.close()
            self.workers.pop(pid,None)
            raise ValueError('项目工作台启动失败，请由管理员检查该项目的 worker.log')
    def close(self):
        # Only stop process trees that this supervisor started.
        with self.lock:
            for worker in self.workers.values():
                if worker.process.poll() is None:
                    if os.name=='nt':
                        subprocess.run(['taskkill','/PID',str(worker.process.pid),'/T','/F'],capture_output=True)
                    else:
                        import signal
                        os.killpg(worker.process.pid,signal.SIGTERM)
                    try: worker.process.wait(timeout=5)
                    except subprocess.TimeoutExpired: worker.process.kill()
                worker.log.close()
            self.workers.clear()
