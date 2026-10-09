from __future__ import annotations
import json
import re
from .web_controls import integrate_workbench

def rewrite_response(content: bytes, media_type: str, prefix: str, csrf: str='') -> bytes:
    if 'text/html' in media_type:
        text=content.decode('utf-8-sig')
        # Existing pages use quoted root URLs in links and JavaScript. Scoped
        # prefixes keep simultaneous projects in separate browser tabs distinct.
        text=re.sub(r'(["\'`])/(?!/)',lambda m:m[1]+prefix+'/',text)
        script='''<script>(function(){const csrf=CSRF;const namespace=PREFIX;if(csrf){for(const method of ['getItem','setItem','removeItem']){const storageMethod=Storage.prototype[method];Storage.prototype[method]=function(key,...args){return storageMethod.call(this,namespace+String(key),...args)}}}const original=window.fetch;window.fetch=function(input,options={}){if(csrf&&new URL(typeof input==='string'?input:input.url,location.href).origin===location.origin){const method=String(options.method||'GET').toUpperCase();if(!['GET','HEAD','OPTIONS'].includes(method)){options={...options,headers:new Headers(options.headers||{})};options.headers.set('X-CSRF-Token',csrf)}}return original.call(this,input,options)}})();</script>'''.replace('CSRF;',json.dumps(csrf)+';').replace('PREFIX;',json.dumps(prefix+'::')+';')
        text=text.replace('<head>','<head>'+script,1)
        if csrf:
            text=integrate_workbench(text,prefix)
            text=text.replace('</body>','<a href="/_portal" style="position:fixed;right:16px;bottom:16px;z-index:9999;background:#fff;color:#176a63;border:1px solid #cad9d5;border-radius:8px;padding:8px 14px;text-decoration:none">返回我的项目</a></body>')
        return text.encode('utf-8')
    if 'application/json' in media_type:
        value=json.loads(content)
        def visit(v):
            if isinstance(v,dict): return {k:visit(x) for k,x in v.items()}
            if isinstance(v,list): return [visit(x) for x in v]
            if isinstance(v,str) and v.startswith(('/api/','/files/','/assets/','/topic-workflow')): return prefix+v
            return v
        return json.dumps(visit(value),ensure_ascii=False).encode('utf-8')
    return content
