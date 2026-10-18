"""Public information/download site around the existing read-only search service."""
from __future__ import annotations
import argparse
import hashlib
import html
import json
import re
import threading
import zipfile
from pathlib import Path,PurePosixPath
from urllib.parse import urlsplit
from fastapi import HTTPException
from fastapi.responses import FileResponse,HTMLResponse,JSONResponse,Response
from pydantic import BaseModel,ConfigDict,Field
from corpus_api.service import CorpusService,SearchEngineCache
from corpus_portal.isolated_search import IsolatedSearchEngine
from .catalog import ReleaseCatalog
from .search import create_app as create_search_app

ROOT=Path(__file__).resolve().parents[1]
WEB=Path(__file__).with_name("web")


class PublicVersion(BaseModel):
    model_config=ConfigDict(extra="forbid")
    corpus_key:str=Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    version:str=Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")


class PublicConfig(BaseModel):
    model_config=ConfigDict(extra="forbid")
    site_title:str=Field(default="中文+ 领域语料库",min_length=1,max_length=100)
    description:str=Field(default="检索已发布语料，了解可复用的本地构建方法。",max_length=1000)
    corpus_title:str=Field(default="领域语料库",max_length=100)
    release_root:str="data/public_site/releases"
    allowed_releases:list[PublicVersion]=Field(default_factory=list)
    source_archive:str=""
    repository_url:str=""
    repository_requires_access:bool=True
    usage_notice:str=Field(default="本站提供检索、标注查询和统计导出。语料及代码使用范围请按维护者说明确认，第三方组件遵循各自许可。",max_length=2000)


def asset_path(project:Path,value:str)->Path:
    path=Path(value)
    return (path if path.is_absolute() else project/path).resolve()


def verify_source_bundle(path:Path):
    manifest_path=path.with_name("release-manifest.json")
    if not path.is_file() or path.suffix.lower()!=".zip" or not manifest_path.is_file():
        raise ValueError("下载包须为发行脚本生成的ZIP，并附带release-manifest.json")
    m=json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if m.get("mode")!="unified-source" or m.get("private_findings")!=0 or any(m.get(key) is not False for key in ("raw_data_included","user_accounts_included","runtime_included")):
        raise ValueError("下载包未通过源码脱敏清单检查")
    with path.open("rb") as stream:digest=hashlib.file_digest(stream,"sha256").hexdigest()
    if digest!=m.get("sha256") or path.stat().st_size!=m.get("bytes"):
        raise ValueError("源码下载包校验失败")
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            parts=PurePosixPath(name).parts
            if not parts or name.startswith('/') or chr(92) in name or ':' in name or '..' in parts:
                raise ValueError("源码包包含不安全的归档路径")
            lower=[part.lower() for part in parts]
            if any(part in {'.git','runtime','.cache'} or (part.startswith('.env') and part!='.env.example') for part in lower):
                raise ValueError("源码包包含运行环境或私有配置")
            if 'data' in lower and lower[-1] not in {'readme.md','.gitkeep'}:
                raise ValueError("源码包包含实际数据，不能提供为公开下载")
    return {"sha256":digest,"bytes":path.stat().st_size,"available":True}


def render(config,title,body):
    page=(WEB/"public.html").read_text(encoding="utf-8-sig")
    for key,value in {"TITLE":html.escape(config.site_title),"PAGE_TITLE":html.escape(title),"DESCRIPTION":html.escape(config.description,quote=True),"USAGE":html.escape(config.usage_notice),"BODY":body}.items():page=page.replace("@@"+key+"@@",value)
    return page


def home_body(config,versions):
    first=versions[0] if versions else {}
    status="已发布 · 可直接检索" if versions else "等待配置公开版本"
    stats=[("公开版本",str(len(versions))),("三级话题",str(first.get("topic_count",0))),("来源文档",str(first.get("document_count",0)))]
    figures=''.join('<div class="stat"><b>'+value+'</b><span>'+name+'</span></div>' for name,value in stats)
    version=html.escape(str(first.get("version","未配置")))
    return f'''<section class="hero"><div><span class="eyebrow">Corpus · Methods · Tools</span><h1>从真实语料出发，<br>发现领域语言规律。</h1><p class="lead">{html.escape(config.description)}</p><div class="actions"><a class="button" href="/search">进入语料库检索 →</a><a class="button secondary" href="/workflow">了解构建流程</a></div></div><aside class="panel collection"><span class="badge">{status}</span><h2 style="margin-top:22px">{html.escape(config.corpus_title)}</h2><p class="muted">当前版本 {version}。支持词频、上下文例句、领域术语、话题体系与来源筛选。</p><div class="stats">{figures}</div></aside></section>
<section class="section"><div class="section-heading"><h2>选择你的使用方式</h2><p>在线查语料，在本地构建自己的语料库。</p></div><div class="cards"><article class="card"><span class="number">01 / EXPLORE</span><h3>查询已发布语料</h3><p>阅读真实例句，查看术语标记和话题名称。按报刊、新闻、论文或具体教材限定来源。</p><a href="/search">打开在线检索 →</a></article><article class="card"><span class="number">02 / UNDERSTAND</span><h3>了解构建方法</h3><p>从文件整理、清洗到话题与术语标注，了解每个步骤的输入、输出和检查要点。</p><a href="/workflow">查看完整流程 →</a></article><article class="card"><span class="number">03 / BUILD LOCALLY</span><h3>下载本地构建工具</h3><p>获取完整代码、安装说明和输入模板，在自己的电脑上处理个人语料，选择适合的模型。</p><a href="/download">获取程序与模板 →</a></article></div></section>
<section class="section note">本网站提供已发布语料的只读检索。个人语料上传、模型配置和加工在下载后的本地工作台完成。检索结果可保存，术语统计可导出CSV。</section>'''


def workflow_body():
    steps=[("准备语料与来源","整理UTF-8 TXT，支持将多个TXT打包为ZIP。每批确认来源类别；教材以每个TXT文件名登记书名。","输入：原文、来源；输出：待加工文件清单。"),
    ("清洗与质量检查","先做基础清洗，按需要选择大模型辅助复核；保留原文和已保存的模型中间结果。","输出：清洗文本、句子与清洗报告。"),
    ("建立或复用话题体系","可直接复用已批准体系，也可运行BERTopic发现聚类，结合参考名称和典型词匹配，再审核发布。","输出：有名称、层级、定义和标志词的话题体系。"),
    ("逐句标注与术语识别","使用最终发布并激活的话题体系做分类；按所选方法识别术语，检查术语与话题证据。","输出：逐句术语、话题标记和审核记录。"),
    ("结构化与索引构建","生成分词、词性和句法等结构化字段，按语料规模配置运行资源，复用已保存结果。","输出：结构化语料与BCC索引。"),
    ("检查、发布与分享","核对检索、来源和标注后生成独立发布版本。个人主机可生成分享链接，服务器也可承载只读检索。","输出：固定版本的Corpus Release与检索入口。")]
    rows=''.join('<li><div><h3>'+name+'</h3><p>'+description+'</p><p><strong>'+output+'</strong></p></div></li>' for name,description,output in steps)
    return '<span class="eyebrow">Reusable workflow</span><h1>把构建方法带回本地。</h1><p class="lead">这是一套可参考、可调整的加工流程。话题体系、术语资源、来源类别和模型都由使用者按自己的领域选择。</p><ol class="flow">'+rows+'</ol><div class="note">本页用于流程说明，不会提交在线加工任务。模型辅助步骤可能需要使用者自行准备接口或模型环境；安装和运行条件见下载包README。</div><div class="actions"><a class="button" href="/download">下载程序与参考模板</a><a class="button secondary" href="/search">看看构建后的检索效果</a></div>'


def download_body(config,bundle):
    if bundle["available"]:
        source='<a class="button" href="/downloads/builder-source.zip">下载完整构建代码 ZIP ↓</a><div class="download-meta">大小：'+f'{bundle["bytes"]/1024**2:.1f} MiB'+'<br>SHA-256：'+bundle["sha256"]+'</div>'
    else:source='<p class="note">维护者尚未配置公开源码下载包。</p>'
    repo=''
    if config.repository_url:
        note='（需要仓库访问权限）' if config.repository_requires_access else ''
        repo='<p><a href="'+html.escape(config.repository_url,quote=True)+'" target="_blank" rel="noopener noreferrer">查看GitHub仓库'+note+'</a></p>'
    return '<span class="eyebrow">Build your own corpus</span><h1>下载工具，构建个人语料库。</h1><p class="lead">源码包包含程序、模板与说明。Python环境、数据库和模型按安装说明另行准备。网站ZIP下载不要求GitHub账号。</p><div class="two"><section class="panel"><h2>完整构建程序</h2>'+source+repo+'<p class="muted">下载包经发行清单和SHA-256校验；当前研究语料、账号及API密钥不随源码分发。</p></section><section class="panel"><h2>开始前准备</h2><ol class="steps"><li>解压后阅读根目录README.md和安装说明。</li><li>按说明创建Python环境、配置数据库，准备所需模型。</li><li>从小规模样本开始，上传自己的TXT及参考体系。</li><li>检查标注后再扩大规模，按需生成只读分享链接。</li></ol></section></div><section class="section"><h2>输入文件参考</h2><table><thead><tr><th>文件</th><th>用途</th><th>下载</th></tr></thead><tbody><tr><td>话题体系填写示例 JSON</td><td>示范三级话题、定义和标志词，可改为自己的领域</td><td><a href="/resources/topic-taxonomy.json">下载示例</a></td></tr><tr><td>规范术语表 CSV</td><td>填写术语编号、名称和别名等字段</td><td><a href="/resources/terminology.csv">下载模板</a></td></tr><tr><td>语料来源登记 CSV</td><td>已有文档的来源类别及具体教材名称登记</td><td><a href="/resources/source-metadata.csv">下载模板</a></td></tr><tr><td>自编短语料 TXT</td><td>演示基础语料输入格式，可选择教材来源体验文件名书名登记</td><td><a href="/resources/sample-corpus.txt">下载样例</a></td></tr></tbody></table></section>'


def api_body():
    return '''<span class="eyebrow">Read-only API</span><h1>在程序中查询语料。</h1><p class="lead">接口与本网站使用同一批明确公开的发布版本，无需构建账号。将示例域名替换为本站实际域名即可。</p><table><thead><tr><th>接口</th><th>用途</th></tr></thead><tbody><tr><td><code>/api/v1/corpora</code></td><td>公开语料库及可选版本</td></tr><tr><td><code>/api/v1/kwic?q=储能&amp;limit=10</code></td><td>上下文例句、术语及话题名称</td></tr><tr><td><code>/api/v1/terms?min_length=4&amp;page_size=10</code></td><td>至少4字符的前十术语频次</td></tr><tr><td><code>/api/v1/terms/export</code></td><td>完整术语频次CSV</td></tr><tr><td><code>/api/v1/topics?level=3</code></td><td>三级话题、定义与标志词</td></tr><tr><td><code>/api/v1/annotations?page_size=20</code></td><td>已保存的逐句术语和话题标注</td></tr><tr><td><code>/api/v1/sources</code></td><td>来源类别及具体教材目录</td></tr></tbody></table><pre>import requests
base = "https://corpus.example.org"
r = requests.get(base + "/api/v1/kwic",
                 params={"q": "储能", "limit": 10}, timeout=30)
r.raise_for_status()
for row in r.json()["result"]["Context"]:
    print(row.get("TopicNames"), row.get("Terms"), row.get("Context"))</pre><p>可选参数：<code>corpus_key</code>、<code>version</code> 指定公开版本；<code>source_category</code> 限定报刊/新闻/论文/教材，<code>book_title</code> 进一步限定具体教材。当前支持的端点与参数以<a href="/openapi.json">OpenAPI描述</a>为准。</p><div class="note">接口提供只读查询和统计。上传、加工、模型管理、私有工作区及原始文件下载没有接入本站。</div>'''


def create_app(config:PublicConfig|dict,*,project_root:Path=ROOT,engine_loader=None):
    config=config if isinstance(config,PublicConfig) else PublicConfig.model_validate(config)
    project_root=Path(project_root).resolve()
    if config.repository_url:
        parsed=urlsplit(config.repository_url)
        if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password:raise ValueError("仓库地址须为不含凭据的HTTPS网址")
    allowed={(v.corpus_key,v.version) for v in config.allowed_releases}
    if len(allowed)!=len(config.allowed_releases):raise ValueError("公开版本配置重复")
    root=asset_path(project_root,config.release_root)
    catalog=ReleaseCatalog(root,allowed=allowed)
    if set(catalog.entries)!=allowed:raise ValueError("明确选定的公开版本缺失或尚未完成索引")
    if len(catalog.corpora)==1:next(iter(catalog.corpora.values()))["name"]=config.corpus_title
    versions=[]
    for chosen in config.allowed_releases:
        release=catalog.resolve_release_path(chosen.corpus_key,chosen.version);base=release/"metadata/knowledge"
        taxonomy=json.loads((base/"taxonomy.json").read_text(encoding="utf-8-sig")) if (base/"taxonomy.json").is_file() else {}
        nodes=taxonomy.get("nodes",[])
        if taxonomy.get("test_only"):raise ValueError("公开站点需要正式话题体系，请先审核或复用正式参考体系")
        docs=set()
        if (base/"documents.jsonl").is_file():
            with (base/"documents.jsonl").open(encoding="utf-8-sig") as stream:
                for line in stream:
                    if line.strip():
                        document=json.loads(line);identity=document.get("doc_id") or document.get("document_id")
                        if identity:docs.add(identity)
        versions.append({"corpus_key":chosen.corpus_key,"version":chosen.version,"taxonomy_version":taxonomy.get("taxonomy_version"),"topic_count":sum(len(str(n.get("path","")).split(" > "))==3 for n in nodes),"document_count":len(docs)})
    archive=asset_path(project_root,config.source_archive) if config.source_archive else None
    bundle=verify_source_bundle(archive) if archive else {"available":False}
    slots=threading.BoundedSemaphore(4)
    service=CorpusService(catalog,SearchEngineCache(loader=engine_loader or (lambda index:IsolatedSearchEngine(index,slots)),max_entries=1))
    app=create_search_app(root,service=service,landing_html=render(config,"首页",home_body(config,versions)))
    @app.middleware("http")
    async def headers(request,call_next):
        response=await call_next(request)
        response.headers["X-Content-Type-Options"]="nosniff"
        response.headers["Referrer-Policy"]="no-referrer"
        response.headers["X-Frame-Options"]="SAMEORIGIN"
        response.headers["Cache-Control"]="no-store"
        return response
    @app.get('/assets/public-site.css',include_in_schema=False)
    def css():return FileResponse(WEB/'public.css',media_type='text/css')
    @app.get('/workflow',response_class=HTMLResponse,include_in_schema=False)
    def workflow():return render(config,"构建流程",workflow_body())
    @app.get('/download',response_class=HTMLResponse,include_in_schema=False)
    def download():return render(config,"下载程序",download_body(config,bundle))
    @app.get('/api-guide',response_class=HTMLResponse,include_in_schema=False)
    def guide():return render(config,"API指南",api_body())
    @app.get('/api/site',include_in_schema=False)
    def info():return {'title':config.site_title,'versions':versions,'source_download':bundle,'read_only':True}
    @app.get('/downloads/builder-source.zip',include_in_schema=False)
    def source_download():
        if not archive:raise HTTPException(404,"维护者尚未配置源码下载包")
        current=verify_source_bundle(archive)
        if current!=bundle:raise HTTPException(503,"下载包已更新，等待维护者校验并重载")
        return FileResponse(archive,filename='corpus-builder-source.zip',media_type='application/zip',headers={'X-Checksum-SHA256':bundle['sha256']})
    @app.get('/downloads/SHA256SUMS.txt',include_in_schema=False)
    def checksum():
        if not archive:raise HTTPException(404,"维护者尚未配置源码下载包")
        return Response(bundle['sha256']+'  corpus-builder-source.zip\n',media_type='text/plain')
    templates={"topic-taxonomy.json":"参考话题体系_填写示例.json","terminology.csv":"规范术语表.csv","source-metadata.csv":"语料来源登记.csv"}
    @app.get('/resources/{name}',include_in_schema=False)
    def resource(name:str):
        if name=='sample-corpus.txt':return Response('水稻种植需要根据生育期调整灌溉。\n土壤水分会影响作物根系生长。\n',media_type='text/plain; charset=utf-8',headers={'Content-Disposition':'attachment; filename="sample-textbook.txt"'})
        if name not in templates:raise HTTPException(404,"模板不存在")
        path=project_root/'参考文件中心/模板'/templates[name]
        if not path.is_file():raise HTTPException(404,"模板尚未安装")
        return FileResponse(path,filename=name,media_type='application/json' if name.endswith('.json') else 'text/csv; charset=utf-8')
    return app


def main(argv=None):
    parser=argparse.ArgumentParser(description='启动公开介绍、只读检索和构建程序下载网站')
    parser.add_argument('--config',type=Path,default=Path('configs/public_site.example.json'))
    parser.add_argument('--project-root',type=Path,default=ROOT)
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8002)
    args=parser.parse_args(argv)
    if not 1<=args.port<=65535:parser.error('端口须为1至65535')
    root=args.project_root.resolve();path=asset_path(root,str(args.config))
    app=create_app(json.loads(path.read_text(encoding='utf-8-sig')),project_root=root)
    import uvicorn
    uvicorn.run(app,host=args.host,port=args.port,workers=1,access_log=False,proxy_headers=False)
    return 0


if __name__=='__main__':raise SystemExit(main())
