"""Inspect failed model items and replay selected requests without restarting runs."""
import threading
from pathlib import Path
from fastapi import APIRouter,Request,HTTPException,Query
from fastapi.responses import FileResponse
from pydantic import BaseModel,Field
from scripts.workflow.model_errors import ModelErrors
from .workbench import require_admin

router=APIRouter()
LOCK=threading.RLock()

class ReplayRequest(BaseModel):
    ids:list[str]=Field(min_length=1,max_length=50)
    api_key:str=Field(min_length=1,max_length=1000)

@router.get('/model-errors',include_in_schema=False)
def page(request:Request):
    require_admin(request)
    return FileResponse(Path(__file__).parent/'web/model-errors.html')

@router.get('/api/model-errors')
def failures(request:Request,page:int=Query(1,ge=1),page_size:int=Query(100,ge=1,le=200)):
    require_admin(request)
    store=ModelErrors()
    return {'page':page,'total':len(list(store.root.glob('*.json'))),'items':[{k:row.get(k) for k in ('id','stage','status','error_type','error','attempts','updated_at','task_index','document_id','sentence_id','request_attempts')}|{'model':row.get('body',{}).get('model','')} for row in store.items((page-1)*page_size,page_size)]}

@router.post('/api/model-errors/retry',status_code=202)
def replay(payload:ReplayRequest,request:Request):
    require_admin(request);store=ModelErrors();selected=[]
    with LOCK:
        for key in dict.fromkeys(payload.ids):
            try:row=store.read(key)
            except ValueError as exc:raise HTTPException(400,str(exc)) from exc
            if not row:raise HTTPException(404,'错误记录不存在')
            if row.get('status') in {'retrying','queued','replayed'}:continue
            selected.append((key,row))
        for key,row in selected:
            row['status']='queued';store.write(key,row)
    def run():
        for key,_ in selected:store.retry(key,payload.api_key)
    if selected:threading.Thread(target=run,daemon=True).start()
    return {'accepted':[key for key,_ in selected],'message':'重跑请求已提交；成功后保留重跑结果，原业务流程的断点续跑负责校验及合并。'}
