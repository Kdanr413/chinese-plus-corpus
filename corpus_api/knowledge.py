"""Read-only linguistic APIs, with an authenticated workspace view."""
from pathlib import Path
from typing import Literal
import csv,json,os,threading,io
from fastapi import Depends,HTTPException,Query,Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel,Field
from .knowledge_assets import KnowledgeAssets,SOURCE_CATEGORIES

PROJECT_ROOT=Path(__file__).resolve().parents[1]
_SOURCE_LOCK=threading.Lock()
class SourceItem(BaseModel):
    document_id:str=Field(min_length=1,max_length=200)
    source_category:Literal['报刊','新闻','论文','教材','未分类']
    book_title:str=Field(default='',max_length=400)
class SourceUpdate(BaseModel):
    items:list[SourceItem]=Field(min_length=1,max_length=2000)

def install_knowledge_routes(app,dependency,*,allow_workspace=False):
    def assets(request,service,dataset,corpus_key,version):
        if dataset=='workspace':
            if not allow_workspace:raise HTTPException(403,'分享入口只开放所选发布版本')
            from .workbench import require_admin
            require_admin(request)
            config=json.loads((PROJECT_ROOT/'configs/pipeline.json').read_text(encoding='utf-8-sig'))
            if corpus_key and corpus_key!=config.get('project',{}).get('corpus_id','chinese-plus'):raise HTTPException(404,'当前项目没有该语料库')
            return KnowledgeAssets(PROJECT_ROOT,True)
        corpus,row=service._resolve_target(corpus_key,version)
        return KnowledgeAssets(service.registry.resolve_release_path(str(corpus['corpus_key']),str(row['version'])))
    def validate_source(value):
        if value and value not in SOURCE_CATEGORIES:raise HTTPException(400,'来源须为报刊、新闻、论文、教材或未分类')
    @app.get('/api/v1/annotations',tags=['Corpus Annotations'],summary='分页读取保存的句子术语与话题标记')
    def annotations(request:Request,dataset:Literal['release','workspace']='release',corpus_key:str|None=Query(default=None,max_length=128),version:str|None=Query(default=None,max_length=128),sentence_id:str=Query(default='',max_length=250),term:str=Query(default='',max_length=250),topic:str=Query(default='',max_length=500),source_category:str=Query(default='',max_length=20),book_title:str=Query(default='',max_length=400),page:int=Query(default=1,ge=1,le=100000),page_size:int=Query(default=50,ge=1,le=200),service=Depends(dependency)):
        validate_source(source_category);a=assets(request,service,dataset,corpus_key,version);start=(page-1)*page_size;items=[];total=0
        for row in a.iter_annotations(source_category,book_title,sentence_id,term,topic):
            if start<=total<start+page_size:items.append(row)
            total+=1
        return {'dataset':dataset,'available':a.annotations.is_file(),'items':items,'total':total,'page':page,'page_size':page_size}
    @app.get('/api/v1/terms',tags=['Corpus Annotations'],summary='查询标注术语的词频和句子频次')
    def terms(request:Request,dataset:Literal['release','workspace']='release',corpus_key:str|None=Query(default=None,max_length=128),version:str|None=Query(default=None,max_length=128),q:str=Query(default='',max_length=250),source_category:str=Query(default='',max_length=20),book_title:str=Query(default='',max_length=400),page:int=Query(default=1,ge=1,le=100000),page_size:int=Query(default=10,ge=1,le=200),service=Depends(dependency)):
        validate_source(source_category);a=assets(request,service,dataset,corpus_key,version);summary=a.summary(source_category,book_title);items=[r for r in summary['terms'] if not q or q in r['term'] or q==r['term_id']];start=(page-1)*page_size
        return {'dataset':dataset,'available':summary['available'],'sentence_count':summary['sentence_count'],'frequency_definition':summary['frequency_definition'],'items':items[start:start+page_size],'total':len(items),'page':page,'page_size':page_size}
    @app.get('/api/v1/terms/export',tags=['Corpus Annotations'],summary='下载当前范围的完整术语频次CSV')
    def export_terms(request:Request,dataset:Literal['release','workspace']='release',corpus_key:str|None=None,version:str|None=None,q:str=Query(default='',max_length=250),source_category:str=Query(default='',max_length=20),book_title:str=Query(default='',max_length=400),service=Depends(dependency)):
        validate_source(source_category);a=assets(request,service,dataset,corpus_key,version)
        summary=a.summary(source_category,book_title)
        def generate():
            buffer=io.StringIO();writer=csv.writer(buffer)
            yield '\ufeff'
            writer.writerow(['term','term_id','frequency','sentence_frequency']);yield buffer.getvalue();buffer.seek(0);buffer.truncate(0)
            for row in summary['terms']:
                if q and q not in row['term'] and q!=row['term_id']:continue
                writer.writerow([row['term'],row['term_id'],row['frequency'],row['sentence_frequency']]);yield buffer.getvalue();buffer.seek(0);buffer.truncate(0)
        return StreamingResponse(generate(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="term_frequency.csv"'})
    @app.get('/api/v1/topics',tags=['Corpus Annotations'],summary='读取正式话题体系及各话题标注单元数')
    def topics(request:Request,dataset:Literal['release','workspace']='release',corpus_key:str|None=Query(default=None,max_length=128),version:str|None=Query(default=None,max_length=128),source_category:str=Query(default='',max_length=20),book_title:str=Query(default='',max_length=400),service=Depends(dependency)):
        validate_source(source_category);a=assets(request,service,dataset,corpus_key,version);taxonomy=json.loads(a.taxonomy.read_text(encoding='utf-8-sig')) if a.taxonomy.is_file() else {};counts=a.summary(source_category,book_title)['topic_counts']
        from .topic_display import TopicDisplay
        nodes=TopicDisplay(a.root,a.workspace).nodes;items=[]
        for n in nodes:
            item=dict(n);path=str(n.get('path') or n.get('full_path') or n.get('name',''));item['sentence_frequency']=counts.get(path,0);item['subtree_sentence_frequency']=sum(v for p,v in counts.items() if p==path or p.startswith(path+' > '));items.append(item)
        return {'dataset':dataset,'available':bool(taxonomy),'taxonomy_version':taxonomy.get('taxonomy_version'),'source_round_id':taxonomy.get('source_round_id'),'status':taxonomy.get('status'),'items':items,'total':len(items),'needs_naming':sum(bool(n.get('needs_name')) for n in nodes),'counts_note':'sentence_frequency为该路径直接标注数；subtree_sentence_frequency为自身及后代路径的标签次数之和，多标签可能重复计数'}
    @app.get('/api/v1/sources',tags=['Corpus Annotations'],summary='查询来源类别、具体教材书名与BCC来源对应')
    def sources(request:Request,dataset:Literal['release','workspace']='release',corpus_key:str|None=Query(default=None,max_length=128),version:str|None=Query(default=None,max_length=128),page:int=Query(default=1,ge=1,le=100000),page_size:int=Query(default=100,ge=1,le=1000),service=Depends(dependency)):
        a=assets(request,service,dataset,corpus_key,version);docs=list(a.documents().values());start=(page-1)*page_size
        return {'dataset':dataset,'items':docs[start:start+page_size],'total':len(docs),'page':page,'page_size':page_size,'categories':[{'name':kind,'document_count':sum(d['source_category']==kind for d in docs)} for kind in SOURCE_CATEGORIES],'textbooks':sorted({d['book_title'] for d in docs if d['source_category']=='教材' and d['book_title']}),'note':'未明确登记的来源为未分类；普通书籍/报告不会自动归为教材。来源筛选进入BCC范围检索，缺少范围索引时明确报错。'}
    if allow_workspace:
        @app.post('/api/v1/source-metadata',tags=['Corpus Annotations'],summary='登记项目来源类别和教材书名；不改写已发布版本')
        def update_sources(payload:SourceUpdate,request:Request):
            from .workbench import require_admin
            require_admin(request);a=KnowledgeAssets(PROJECT_ROOT,True);known=a.documents()
            if len({x.document_id for x in payload.items})!=len(payload.items):raise HTTPException(400,'同一批次文档编号重复')
            for item in payload.items:
                if item.document_id not in known:raise HTTPException(400,'文档编号不属于当前项目')
                if item.source_category=='教材' and not item.book_title.strip():raise HTTPException(400,'教材必须填写具体书名')
            with _SOURCE_LOCK:
                mapping={}
                if a.mapping.is_file():
                    with a.mapping.open(encoding='utf-8-sig',newline='') as h:mapping={r['document_id']:r for r in csv.DictReader(h)}
                for item in payload.items:mapping[item.document_id]=item.model_dump()
                a.mapping.parent.mkdir(parents=True,exist_ok=True);temp=a.mapping.with_suffix('.csv.tmp')
                if a.mapping.is_file():
                    import shutil
                    from datetime import datetime
                    backup=PROJECT_ROOT/'data/reference_archive'/('sources-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'));backup.mkdir(parents=True);shutil.copy2(a.mapping,backup/'sources.csv')
                from .source_metadata import write_csv_atomic
                write_csv_atomic(a.mapping,['document_id','source_category','book_title'],mapping.values())
            return {'ok':True,'updated':len(payload.items),'message':'已保存项目来源信息；正式版本需在未来发布时冻结，旧版本保持不变'}
