from __future__ import annotations
import argparse
import os
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from corpus_api.service import CorpusService, SearchEngineCache, SearchRequestError, SearchTargetError, SearchAssetUnavailableError, CorpusServiceUnavailableError
from corpus_api.models import CorpusListResponse, CorpusVersionListResponse, SearchResponse
from corpus_registry import RegistryNotFoundError
from .catalog import ReleaseCatalog

WEB_ROOT = Path(__file__).resolve().parents[1] / 'corpus_api' / 'web'

def create_app(root: Path, *, service=None):
    service = service or CorpusService(ReleaseCatalog(root), SearchEngineCache())
    app = FastAPI(title='语料库只读检索', docs_url=None, redoc_url=None, openapi_url=None)
    @app.exception_handler(Exception)
    async def failure(request, exc):
        # Never disclose server paths or configuration in public responses.
        if isinstance(exc, RegistryNotFoundError):
            status, code = 404, 'registry_not_found'
        elif isinstance(exc, SearchRequestError):
            status, code = 400, 'search_request_invalid'
        elif isinstance(exc, SearchTargetError):
            status, code = 409, 'search_target_invalid'
        else:
            status, code = 503, 'search_asset_unavailable'
        return JSONResponse(status_code=status, content={'error': {'code': code, 'message': '查询暂时不可用，请检查输入或联系管理员。'}})
    @app.get('/health')
    def health():
        return {'status': 'ok', 'service': 'corpus-readonly-search', 'api_version': '1.0'}
    @app.get('/', response_class=HTMLResponse)
    @app.get('/search', response_class=HTMLResponse)
    def page():
        html = (WEB_ROOT / 'search.html').read_text(encoding='utf-8-sig')
        html = html.replace('<a href="/">语料库构建</a>', '')
        html = html.replace('系统自动读取 PostgreSQL Registry 中可检索的 Release。', '选择已发布的语料库与版本。')
        html = html.replace('再保存结果或导出结构化语料', '再保存查询结果')
        html = html.replace('保存本次查询结果，或导出当前已发布版本的结构化语料。', '保存本次查询结果。')
        html = html.replace('<a id="exportStructured" class="secondary export" href="#">导出当前版本结构化语料</a>', '<a id="exportStructured" hidden></a>')
        html = html.replace('语料库操作台 - ', '')
        return html
    @app.get('/assets/ui-polish.css')
    def css():
        return FileResponse(WEB_ROOT / 'ui-polish.css', media_type='text/css')
    @app.get('/docs', response_class=HTMLResponse)
    def guide():
        return '<html lang="zh"><meta charset="utf-8"><title>检索说明</title><h1>语料库检索</h1><p>选择语料库和版本，输入词语或 BCC 检索式，查询词频或上下文，并保存查询结果。</p><p>本服务只开放检索，不提供上传、加工、删除或整个语料库下载。</p><a href="/search">返回检索</a></html>'
    @app.get('/api/v1/corpora', response_model=CorpusListResponse)
    def corpora():
        items = service.list_corpora()
        return {'items': items, 'count': len(items)}
    @app.get('/api/v1/corpora/{corpus_id}/versions', response_model=CorpusVersionListResponse)
    def versions(corpus_id: str):
        items = service.list_versions(corpus_id)
        return {'corpus_id': corpus_id, 'items': items, 'count': len(items)}
    @app.get('/api/v1/search', response_model=SearchResponse)
    def search(q: str = Query(min_length=1, max_length=500), corpus_key: str | None = Query(default=None, max_length=128), version: str | None = Query(default=None, max_length=128), limit: int = Query(default=100, ge=1, le=1000)):
        return service.search(q, corpus_key=corpus_key, version=version, limit=limit)
    @app.get('/api/v1/kwic', response_model=SearchResponse)
    def kwic(q: str = Query(min_length=1, max_length=500), corpus_key: str | None = Query(default=None, max_length=128), version: str | None = Query(default=None, max_length=128), limit: int = Query(default=20, ge=1, le=100), page: int = Query(default=0, ge=0, le=100000), window: int = Query(default=50, ge=1, le=200)):
        return service.kwic(q, corpus_key=corpus_key, version=version, limit=limit, page=page, window=window)
    return app

def main():
    p = argparse.ArgumentParser(description='Start a standalone read-only BCC search service')
    p.add_argument('--releases', type=Path, default=Path('data/releases'))
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8001)
    p.add_argument('--open-browser', action='store_true')
    a = p.parse_args()
    app = create_app(a.releases)
    if not ReleaseCatalog(a.releases).entries:
        raise SystemExit('No indexed Releases found; copy a data package into data/releases first')
    if a.open_browser:
        def open_ready():
            for _ in range(80):
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{a.port}/health', timeout=.5):
                        webbrowser.open(f'http://127.0.0.1:{a.port}/search')
                        return
                except OSError:
                    time.sleep(.25)
        threading.Thread(target=open_ready, daemon=True).start()
    import uvicorn
    uvicorn.run(app, host=a.host, port=a.port, access_log=False)

if __name__ == '__main__':
    main()

