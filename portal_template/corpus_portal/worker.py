from __future__ import annotations
import argparse
import json
import os
import secrets
import sys
from pathlib import Path
from .workspaces import PayloadValidationError,validate_json_payload

def allowed_python_executables():
    # Use the same administrator-selected or discovered interpreter as the UI.
    from corpus_api.topic_workflow import _recommended_python
    return {str(Path(sys.executable).resolve()),str(Path(_recommended_python()).resolve())}

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--project',type=Path,required=True)
    p.add_argument('--port',type=int,required=True)
    a=p.parse_args()
    root=a.project.resolve()
    if not os.environ.get('CORPUS_DB_SCHEMA','').startswith('p_'):
        raise SystemExit('Project database schema is required')
    secret=os.environ['CORPUS_INTERNAL_TOKEN']
    os.chdir(root)
    sys.path.insert(0,str(root))
    os.environ['CORPUS_PROJECT_ROOT']=str(root)
    os.environ['CORPUS_DATA_ROOT']='data'
    os.environ['ADMIN_API_TOKEN']=secret
    from corpus_registry import CorpusRegistry, RegistryNotFoundError, initialize_database
    initialize_database()
    registry=CorpusRegistry()
    try: registry.get_corpus('chinese-plus')
    except RegistryNotFoundError: registry.create_corpus('chinese-plus','我的语料库')
    from corpus_delivery.catalog import ReleaseCatalog
    catalog=ReleaseCatalog(root/'data/releases')
    for (key,version),(release,manifest,row) in catalog.entries.items():
        try: registry.get_corpus(key)
        except RegistryNotFoundError: registry.create_corpus(key,key)
        try: registry.get_version(key,version)
        except RegistryNotFoundError:
            registry.register_release(corpus_key=key,version=version,release_path=release,corpus_format_version=manifest['corpus_format_version'],pipeline_version=manifest['pipeline_version'],search_engine_version=manifest['search_engine_version'],status='published',searchable=True,make_default=True)
    from corpus_api.app import app
    from fastapi.responses import JSONResponse
    @app.middleware('http')
    async def internal_only(request,call_next):
        if not secrets.compare_digest(request.headers.get('x-corpus-internal',''),secret):
            return JSONResponse({'error':'不可直接访问内部构建服务'},status_code=403)
        if request.method not in {'GET','HEAD','OPTIONS'} and 'json' in request.headers.get('content-type',''):
            try:
                allowed=allowed_python_executables()
                validate_json_payload(await request.body(),root,allowed)
            except PayloadValidationError as exc:
                return JSONResponse({'error':str(exc)},status_code=400)
            except (ValueError,TypeError):
                return JSONResponse({'error':'项目路径或运行参数无效'},status_code=400)
        return await call_next(request)
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=a.port,workers=1,proxy_headers=False,access_log=False)

if __name__=='__main__': main()
