from __future__ import annotations
import json
import re

def integrate_workbench(text: str, prefix: str) -> str:
    """Reuse the portal share dialog inside the original sixth workflow step."""
    match=re.fullmatch(r'/p/([0-9a-f-]{36})',prefix)
    if not match or '<section id="publish"' not in text: return text
    pid=match[1]
    panel='''<div class="message" style="margin-top:18px"><h3>分享已发布的语料库</h3><p>发布并建立索引后，可以生成只读检索链接。接收者无需登录，即可检索所选版本。</p><button id="portalShareButton" type="button" class="secondary">分享检索 / 管理链接</button></div>'''
    text=re.sub(r'(<section id="publish"[^>]*>.*?)(</section>)',lambda m:m[1]+panel+m[2],text,count=1,flags=re.S)
    dialog='''<dialog id="portalShareDialog" style="width:min(760px,94vw);padding:12px;border:1px solid #cad9d5;border-radius:14px"><div style="display:flex;justify-content:space-between;align-items:center;padding:8px"><b>语料库分享</b><button id="portalShareClose" type="button" class="secondary">关闭</button></div><iframe id="portalShareFrame" title="分享已发布语料库" style="width:100%;height:min(620px,72vh);border:0"></iframe></dialog>
<script>(function(){const project=PROJECT;const dialog=document.getElementById('portalShareDialog'),frame=document.getElementById('portalShareFrame');document.getElementById('portalShareButton').onclick=function(){const version=document.getElementById('publishVersion')?.value||'';frame.src='/_portal?embedded=1&share='+encodeURIComponent(project)+'&version='+encodeURIComponent(version);dialog.showModal()};document.getElementById('portalShareClose').onclick=function(){dialog.close()};window.addEventListener('message',function(event){if(event.origin===location.origin&&event.source===frame.contentWindow&&event.data?.kind==='corpus-share-close')dialog.close()})})();</script>'''.replace('PROJECT;',json.dumps(pid)+';')
    return text.replace('</body>',dialog+'</body>',1)
