from __future__ import annotations
import asyncio
import hmac
import json
import re
import shutil
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from corpus_delivery.catalog import ReleaseCatalog
from corpus_delivery.search import create_app as create_search_app
from corpus_api.service import CorpusService, SearchEngineCache
from .isolated_search import IsolatedSearchEngine
from .store import PortalStore, normalized_project_name, project_name_key
from .supervisor import WorkerSupervisor
from .workspaces import PortalSettings, initialize_workspace
from .storage import UPLOAD_PATHS, quota_error
from .security import origin_matches
from .rewrite import rewrite_response

WEB=Path(__file__).with_name('web')
COOKIE='corpus_portal_session'

class Login(BaseModel):
    username: str=Field(min_length=3,max_length=64)
    password: str=Field(min_length=3,max_length=128)
class NewProject(BaseModel):
    name: str=Field(min_length=1,max_length=120)
class NewShare(BaseModel):
    corpus_key: str=Field(default='chinese-plus',min_length=1,max_length=128)
    version: str=Field(min_length=1,max_length=128)
    expires_days: int | None=Field(default=None,ge=1,le=365)

def create_app(settings=None,store=None,supervisor=None,share_loader=None):
    settings=settings or PortalSettings.environment()
    store=store or PortalStore()
    supervisor=supervisor or WorkerSupervisor(settings)
    query_slots=threading.BoundedSemaphore(settings.max_workers)
    @asynccontextmanager
    async def lifespan(app):
        yield
        await asyncio.to_thread(supervisor.close)
    app=FastAPI(title='多用户语料库平台',docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    shares={}
    def user(request):
        session=store.session(request.cookies.get(COOKIE,''))
        if not session: raise HTTPException(401,'请先登录')
        if request.method not in {'GET','HEAD','OPTIONS'}:
            origin=request.headers.get('origin')
            if origin and not origin_matches(origin,settings.public_url): raise HTTPException(403,'请求来源无效')
            if not hmac.compare_digest(request.headers.get('x-csrf-token',''),session['csrf_token']): raise HTTPException(403,'操作验证失败，请刷新页面')
        return session
    def owned(request,pid):
        u=user(request)
        row=store.project(u['id'],pid)
        if row is None: raise HTTPException(404,'项目不存在或不可访问')
        return u,row
    @app.exception_handler(Exception)
    async def failure(request,exc):
        return JSONResponse({'detail':'服务暂时不可用，请联系管理员查看服务器日志'},status_code=503)
    @app.get('/health')
    def health(): return {'status':'ok','service':'corpus-portal'}
    @app.get('/',include_in_schema=False)
    def home(): return RedirectResponse('/_portal')
    @app.get('/_portal',response_class=HTMLResponse)
    def page(): return (WEB/'index.html').read_text(encoding='utf-8-sig')
    @app.post('/_portal/api/login')
    def login(payload:Login,request:Request):
        if request.headers.get('origin') and not origin_matches(request.headers['origin'],settings.public_url): raise HTTPException(403,'请求来源无效')
        peer=request.client.host if request.client else 'unknown'
        result=store.login(payload.username,payload.password,peer)
        if result is None: raise HTTPException(401,'账号或密码错误，或尝试次数过多，请稍后重试')
        token,csrf=result
        response=JSONResponse({'csrf_token':csrf})
        response.set_cookie(COOKIE,token,httponly=True,secure=settings.secure_cookie,samesite='lax',path='/')
        response.headers['Cache-Control']='no-store'
        return response
    @app.get('/_portal/api/me')
    def me(request:Request):
        u=user(request)
        return {'username':u['username'],'csrf_token':u['csrf_token']}
    @app.post('/_portal/api/logout')
    def logout(request:Request):
        user(request)
        store.logout(request.cookies.get(COOKIE,''))
        response=JSONResponse({'ok':True})
        response.delete_cookie(COOKIE,path='/')
        return response
    @app.get('/_portal/api/projects')
    def list_projects(request:Request): return {'items':store.list_projects(user(request)['id'])}
    @app.post('/_portal/api/projects',status_code=201)
    def new_project(payload:NewProject,request:Request):
        u=user(request)
        name=normalized_project_name(payload.name)
        if not name or '\x00' in name: raise HTTPException(400,'项目名称无效')
        existing=store.list_projects(u['id'])
        if any(project_name_key(row['name'])==project_name_key(name) for row in existing): raise HTTPException(409,'已有同名语料库项目，请重新命名')
        if len(existing)>=5: raise HTTPException(409,'每个账号最多创建 5 个项目')
        pid=uuid.uuid4()
        project_root=settings.project_root(pid)
        fresh=not project_root.exists()
        initialize_workspace(settings,pid,name)
        try: store.add_project(u['id'],pid,name)
        except ValueError as exc:
            if fresh and project_root.is_relative_to(settings.storage.resolve()) and project_root.exists(): shutil.rmtree(project_root)
            raise HTTPException(409,str(exc)) from exc
        return {'id':str(pid),'name':name,'workbench_url':f'/p/{pid}/'}
    @app.get('/_portal/api/network-info')
    def network_info(request:Request):
        user(request)
        import socket
        from urllib.parse import urlsplit
        host=urlsplit(settings.public_url).hostname or ''
        try:addresses=sorted({row[4][0] for row in socket.getaddrinfo(socket.gethostname(),None,socket.AF_INET) if not row[4][0].startswith(('127.','169.254.'))})
        except OSError:addresses=[]
        return {'public_url':settings.public_url,'loopback_url':host in {'localhost','127.0.0.1','::1'},'listen_host':settings.bind_host,'lan_addresses':addresses,'note':'127.0.0.1只能本机访问。跨电脑需配置API_HOST与PORTAL_PUBLIC_URL，并检查防火墙/服务器入口；本接口不保证远程可达。'}
    @app.get('/_portal/api/projects/{pid}/versions')
    def versions(pid:uuid.UUID,request:Request):
        owned(request,pid)
        catalog=ReleaseCatalog(settings.project_root(pid)/'data/releases')
        return {'items':[{'corpus_key':key,'version':version} for key,version in catalog.entries]}
    @app.get('/_portal/api/projects/{pid}/shares')
    def list_shares(pid:uuid.UUID,request:Request):
        u,_=owned(request,pid)
        return {'items':store.list_shares(u['id'],pid)}
    @app.post('/_portal/api/projects/{pid}/shares',status_code=201)
    def publish_share(pid:uuid.UUID,payload:NewShare,request:Request):
        u,_=owned(request,pid)
        catalog=ReleaseCatalog(settings.project_root(pid)/'data/releases',allowed={(payload.corpus_key,payload.version)})
        if (payload.corpus_key,payload.version) not in catalog.entries: raise HTTPException(409,'只能分享校验通过且已建立索引的正式版本')
        row=catalog.get_version(payload.corpus_key,payload.version)
        expires=datetime.now(timezone.utc)+timedelta(days=payload.expires_days) if payload.expires_days else None
        sid,token=store.add_share(u['id'],pid,payload.corpus_key,payload.version,row['checksum_sha256'],expires)
        return {'id':str(sid),'url':settings.public_url+f'/share/{token}/search','version':payload.version,'expires_at':expires}
    @app.post('/_portal/api/shares/{sid}/revoke')
    def revoke(sid:uuid.UUID,request:Request):
        if not store.revoke_share(user(request)['id'],sid): raise HTTPException(404,'分享不存在或不可访问')
        return {'ok':True}
    async def body(request):
        chunks=[]
        size=0
        async for chunk in request.stream():
            size+=len(chunk)
            if size>settings.max_body: raise HTTPException(413,'请求过大，请分批上传')
            chunks.append(chunk)
        return b''.join(chunks)
    def response(upstream,prefix,csrf=''):
        content_type=upstream.headers.get('content-type','')
        content=rewrite_response(upstream.content,content_type,prefix,csrf)
        headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer'}
        for key in ['content-type','content-disposition']:
            if key in upstream.headers: headers[key]=upstream.headers[key]
        if 'location' in upstream.headers:
            location=upstream.headers['location']
            headers['location']=prefix+location if location.startswith('/') else location
        return Response(content,status_code=upstream.status_code,headers=headers)
    @app.api_route('/p/{pid}/{path:path}',methods=['GET','POST','PUT','PATCH','DELETE','HEAD'])
    async def project_proxy(pid:uuid.UUID,path:str,request:Request):
        u,_=owned(request,pid)
        if any(part in {'.','..'} for part in path.split('/')): raise HTTPException(400,'路径无效')
        content=await body(request)
        if request.method=='POST' and path.rstrip('/') in UPLOAD_PATHS:
            error=await asyncio.to_thread(quota_error,settings.project_root(pid),len(content),settings.project_quota)
            if error: raise HTTPException(413,error)
        try: worker=await asyncio.to_thread(supervisor.ensure,pid)
        except ValueError as exc: raise HTTPException(503,str(exc)) from exc
        headers={'x-corpus-internal':worker.token,'x-admin-token':worker.token}
        if 'content-type' in request.headers: headers['content-type']=request.headers['content-type']
        async with httpx.AsyncClient(timeout=httpx.Timeout(180,connect=5),trust_env=False) as client:
            try:
                result=await client.request(request.method,f'http://127.0.0.1:{worker.port}/'+path,params=list(request.query_params.multi_items()),headers=headers,content=content)
            except httpx.TimeoutException as exc:
                raise HTTPException(504,'项目服务响应超时，操作可能已被后台接受；请先查看任务状态，不要重复提交。') from exc
            except httpx.RequestError as exc:
                raise HTTPException(502,'暂时无法连接项目服务，请稍后刷新检查任务状态；不要重复提交。') from exc
        return response(result,f'/p/{pid}',u['csrf_token'])
    @app.get('/share/{token}/{path:path}')
    async def share_proxy(token:str,path:str,request:Request):
        if not re.fullmatch(r'[A-Za-z0-9_-]{40,64}',token): raise HTTPException(404,'分享不存在或已失效')
        record=store.share(token)
        if record is None: raise HTTPException(404,'分享不存在或已失效')
        if not (path in {'','search','docs','openapi.json','health','assets/ui-polish.css','api/v1/corpora','api/v1/search','api/v1/kwic','api/v1/annotations','api/v1/terms','api/v1/terms/export','api/v1/topics','api/v1/sources'} or re.fullmatch(r'api/v1/corpora/[0-9a-f-]{36}/versions',path)):
            raise HTTPException(404,'分享入口只提供检索')
        cache_key=str(record['id'])
        if cache_key not in shares:
            if share_loader:
                scoped=share_loader(record)
            else:
                root=settings.project_root(record['project_id'])/'data/releases'
                catalog=ReleaseCatalog(root,allowed={(record['corpus_key'],record['version'])})
                row=catalog.get_version(record['corpus_key'],record['version'])
                if row['checksum_sha256']!=record['checksum_sha256']: raise HTTPException(409,'分享版本校验不一致')
                scoped=create_search_app(root,service=CorpusService(catalog,SearchEngineCache(loader=lambda index: IsolatedSearchEngine(index,query_slots),max_entries=1)))
            # Bound memory: never keep an unlimited number of native BCC indexes.
            if len(shares)>=settings.max_workers: shares.pop(next(iter(shares)))
            shares[cache_key]=scoped
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=shares[cache_key],raise_app_exceptions=False),base_url='http://share',trust_env=False) as client:
            result=await client.get('/'+path,params=list(request.query_params.multi_items()))
        return response(result,f'/share/{token}')
    return app
