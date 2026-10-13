"""Private per-project failure ledger and replayable model requests."""
from __future__ import annotations
import hashlib,json,os,tempfile,urllib.request
from pathlib import Path
from datetime import datetime,timezone
from .file_paths import io_path
from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode

def project_root():return Path(os.environ.get('CORPUS_PROJECT_ROOT') or Path(__file__).resolve().parents[2])

class ModelErrors:
    def __init__(self,root=None):self.root=io_path(Path(root or project_root())/'data/model_errors')
    def record(self,stage,url,body,error,secret='',extra=None):
        sensitive={'api_key','authorization','password','access_token','token'}
        def safe(value):
            if isinstance(value,dict):return {k:safe(v) for k,v in value.items() if k.lower() not in sensitive}
            if isinstance(value,list):return [safe(v) for v in value]
            return value
        body=safe(body)
        parsed=urlsplit(url)
        netloc=parsed.netloc.rsplit('@',1)[-1]
        url=urlunsplit((parsed.scheme,netloc,parsed.path,urlencode([(k,v) for k,v in parse_qsl(parsed.query,keep_blank_values=True) if k.lower() not in sensitive]),''))
        identity={'stage':stage,'url':url,'body':body}
        key=hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        message=str(error).replace(secret,'[REDACTED]') if secret else str(error)
        old=self.read(key) or {}
        row={**old,**identity,'id':key,'status':'failed','error_type':type(error).__name__,'error':message[:2000],'updated_at':datetime.now(timezone.utc).isoformat(),'attempts':old.get('attempts',0)+1,**(extra or {})}
        self.write(key,row);return key
    def read(self,key):
        if len(key)!=64 or any(c not in '0123456789abcdef' for c in key):raise ValueError('错误记录编号无效')
        try:return json.loads((self.root/(key+'.json')).read_text(encoding='utf-8'))
        except FileNotFoundError:return None
    def write(self,key,row):
        self.root.mkdir(parents=True,exist_ok=True)
        fd,name=tempfile.mkstemp(dir=self.root,suffix='.tmp')
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(row,f,ensure_ascii=False)
            os.replace(name,self.root/(key+'.json'))
        finally:
            if os.path.exists(name):os.unlink(name)
    def items(self,offset=0,limit=100):
        rows=[]
        if not self.root.exists():return rows
        paths=sorted(self.root.glob('*.json'),key=lambda path:path.stat().st_mtime_ns,reverse=True)
        for path in paths[offset:offset+limit]:
            try:row=json.loads(path.read_text(encoding='utf-8'))
            except (OSError,ValueError):continue
            rows.append(row)
        return sorted(rows,key=lambda row:row.get('updated_at',''),reverse=True)
    def retry(self,key,api_key):
        row=self.read(key)
        if not row:raise ValueError('错误记录不存在')
        if not api_key:raise ValueError('重跑需要重新输入 API Key')
        row.update(status='retrying',updated_at=datetime.now(timezone.utc).isoformat());self.write(key,row)
        try:
            if os.environ.get('CORPUS_DB_SCHEMA','').startswith('p_'):
                from corpus_portal.workspaces import verify_payload
                verify_payload({'base_url':row['url']},project_root(),set())
            request=urllib.request.Request(row['url'],data=json.dumps(row['body'],ensure_ascii=False).encode(),headers={'Authorization':'Bearer '+api_key,'Content-Type':'application/json'},method='POST')
            with urllib.request.urlopen(request,timeout=180) as response:payload=json.loads(response.read().decode('utf-8'))
            from corpus_topic_workflow.llm_annotation_api import _extract_text
            from .prompting import parse_json_content
            content=_extract_text(payload,'chat_completions' if 'messages' in row['body'] else 'responses')
            content=content.replace(api_key,'[REDACTED]')
            parsed=parse_json_content(content)
            if not isinstance(parsed,(dict,list)):raise ValueError('模型结果必须是 JSON 对象或数组')
            row.update(status='replayed',result=parsed,raw_content=content,error='',updated_at=datetime.now(timezone.utc).isoformat())
            if row.get('cache_key'):
                from .model_cache import AnnotationCache
                AnnotationCache(project_root()/'data/model_annotations').put(row['cache_key'],row.get('bindings',{}),content)
        except Exception as error:
            row.update(status='failed',error_type=type(error).__name__,error=str(error).replace(api_key,'[REDACTED]')[:2000],updated_at=datetime.now(timezone.utc).isoformat())
        row['attempts']=row.get('attempts',0)+1;self.write(key,row)
        return row

def main():
    import argparse,getpass
    parser=argparse.ArgumentParser(description='查看模型失败条目或只重跑指定请求，不重启整轮加工')
    parser.add_argument('--project',type=Path,default=Path.cwd())
    parser.add_argument('--retry',help='错误记录的64位编号；未指定时列出最新100条')
    args=parser.parse_args();root=args.project.resolve()
    if not (root/'configs/pipeline.json').is_file():parser.error('请指定包含 configs/pipeline.json 的项目目录')
    os.environ['CORPUS_PROJECT_ROOT']=str(root)
    store=ModelErrors(root)
    if not args.retry:
        for row in store.items():print(json.dumps({k:row.get(k) for k in ('id','stage','status','error','updated_at')},ensure_ascii=False))
        return 0
    row=store.read(args.retry)
    if not row:parser.error('错误记录不存在')
    if row.get('status') in ('queued','retrying','replayed'):parser.error('条目已在重跑或已成功返回，请从原流程校验合并，不要重复提交')
    result=store.retry(args.retry,getpass.getpass('API Key（不会保存）：'))
    print(json.dumps({'id':args.retry,'status':result['status'],'error':result.get('error','')},ensure_ascii=False))
    return 0 if result['status']=='replayed' else 1

if __name__=='__main__':raise SystemExit(main())
