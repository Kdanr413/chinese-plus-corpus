"""Project-local, content-addressed model annotations; never stores API keys."""
from __future__ import annotations
import hashlib
import json
import os
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from .prompting import chat_completions_url, parse_json_content

REFERENCE_KEYS={'doc_id','document_id','paragraph_id','sentence_id','run_id','task_index'}

def normalize_messages(messages):
    bindings={}
    def visit(value,path=''):
        if isinstance(value,dict):
            return {k:(reference(v,path+'/'+k) if k in REFERENCE_KEYS else visit(v,path+'/'+k)) for k,v in value.items()}
        if isinstance(value,list):return [visit(v,path+'/'+str(i)) for i,v in enumerate(value)]
        return value
    def reference(value,path):
        if isinstance(value,(str,int)):
            bindings[path]=str(value)
            return '__reference__'+path
        return value
    result=[]
    for index,message in enumerate(messages):
        content=message['content']
        for marker in ('任务数据：\n','输入：\n'):
            if marker not in content:continue
            prefix,tail=content.rsplit(marker,1)
            try:
                stripped=tail.lstrip();data,end=json.JSONDecoder().raw_decode(stripped)
            except ValueError:continue
            normalized=visit(data,str(index))
            content=prefix+marker+json.dumps(normalized,ensure_ascii=False,sort_keys=True,separators=(',',':'))+stripped[end:]
            break
        result.append({**message,'content':content})
    return result,bindings

class AnnotationCache:
    def __init__(self,root):self.root=Path(root)
    def identify(self,base_url,model,messages,model_version=''):
        normalized,bindings=normalize_messages(messages)
        request={'format':1,'endpoint':chat_completions_url(base_url),'model':model,'model_version':model_version or model,'temperature':0.1,'messages':normalized}
        key=hashlib.sha256(json.dumps(request,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        return key,bindings
    def path(self,key):return self.root/key[:2]/(key+'.json')
    def get(self,key,bindings):
        try:entry=json.loads(self.path(key).read_text(encoding='utf-8'))
        except (OSError,ValueError):return None
        content=entry.get('content')
        if entry.get('key')!=key or not isinstance(content,str):return None
        if hashlib.sha256(content.encode()).hexdigest()!=entry.get('content_sha256'):return None
        # Rebind location identifiers to the current task, preserving the cached
        # wording, labels and evidence rather than copying the previous task ID.
        try:parsed=parse_json_content(content)
        except (ValueError,TypeError):return None
        replacements={old:bindings[path] for path,old in entry.get('bindings',{}).items() if path in bindings and old}
        def visit(value):
            if isinstance(value,dict):return {k:visit(v) for k,v in value.items()}
            if isinstance(value,list):return [visit(v) for v in value]
            if isinstance(value,str):return replacements.get(value,value)
            return value
        return json.dumps(visit(parsed),ensure_ascii=False)
    def put(self,key,bindings,content):
        try:parsed=parse_json_content(content)
        except (ValueError,TypeError):return False
        if not isinstance(parsed,(dict,list)):return False
        target=self.path(key);target.parent.mkdir(parents=True,exist_ok=True)
        entry={'key':key,'created_at':datetime.now(timezone.utc).isoformat(),'bindings':bindings,'content':content,'content_sha256':hashlib.sha256(content.encode()).hexdigest()}
        fd,name=tempfile.mkstemp(dir=target.parent,suffix='.tmp')
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as stream:
                json.dump(entry,stream,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
            os.replace(name,target)
        finally:
            if os.path.exists(name):os.unlink(name)
        return True
