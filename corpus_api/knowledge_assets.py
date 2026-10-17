"""Version-scoped, streaming access to saved linguistic annotations."""
from __future__ import annotations
import csv,json,threading
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

SOURCE_CATEGORIES=('报刊','新闻','论文','教材','未分类')
_SUMMARY_LOCK=threading.Lock()
ALIASES={'newspaper':'报刊','press':'报刊','news':'新闻','paper':'论文','journal':'论文','academic':'论文','textbook':'教材','book_textbook':'教材'}

def rows(path):
    if path.is_file():
        with path.open(encoding='utf-8-sig') as stream:
            for line in stream:
                if line.strip():yield json.loads(line)

def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else {}

def category(value):
    value=str(value or '').strip()
    return value if value in SOURCE_CATEGORIES else ALIASES.get(value,'未分类')

class KnowledgeAssets:
    def __init__(self,root:Path,workspace=False):
        self.root=root.resolve();self.workspace=workspace
        self.upload_mapping=self.root/"inputs/metadata/upload_sources.csv"
        base=self.root/'data' if workspace else self.root/'metadata/knowledge'
        self.annotations=base/('curated/annotations/sentence_annotations.jsonl' if workspace else 'sentence_annotations.jsonl')
        self.taxonomy=base/('curated/topics/taxonomy.json' if workspace else 'taxonomy.json')
        self.terminology=base/('curated/terms/terminology.csv' if workspace else 'terminology.csv')
        self.doc_path=base/('processed/corpus/jsonl/documents.jsonl' if workspace else 'documents.jsonl')
        self.mapping=self.root/('inputs/metadata/sources.csv' if workspace else 'metadata/knowledge/sources.csv')

    def signature(self):
        return tuple((str(p),p.stat().st_mtime_ns,p.stat().st_size) if p.is_file() else (str(p),0,0) for p in (self.annotations,self.taxonomy,self.terminology,self.doc_path,self.mapping,self.upload_mapping))

    def documents(self):
        result={}
        from .source_metadata import upload_source_map
        uploaded=upload_source_map(self.root) if self.workspace else {}
        for row in rows(self.doc_path):
            key=str(row.get('doc_id',row.get('document_id','')))
            value={'document_id':key,'title':str(row.get('title','')),'source_category':category(row.get('source_category') or row.get('source_type') or row.get('source')),'book_title':str(row.get('book_title',''))}
            original=Path(str(row.get("original_path") or ""))
            original=original.resolve() if original.is_absolute() else (self.root/original).resolve()
            key_path=original.relative_to(self.root).as_posix() if original.is_relative_to(self.root) else ""
            if key_path in uploaded:
                value.update(source_category=category(uploaded[key_path].get("source_category")),book_title=uploaded[key_path].get("book_title", ""))
            # A textbook must have a specific title; never infer one from a filename.
            if value['source_category']=='教材' and not value['book_title']:value['source_category']='未分类'
            result[key]=value
        if self.mapping.is_file():
            with self.mapping.open(encoding='utf-8-sig',newline='') as stream:
                for row in csv.DictReader(stream):
                    key=str(row.get('document_id',''))
                    if key in result:
                        value=result[key];value['source_category']=category(row.get('source_category'));value['book_title']=str(row.get('book_title','')).strip()
                        if value['source_category']=='教材' and not value['book_title']:value['source_category']='未分类'
        if not self.workspace:
            for row in read_json(self.root/'metadata/selection.json').get('corpus_files',[]):
                key=str(row.get('document_id',''))
                result.setdefault(key,{'document_id':key,'title':str(row.get('document_name','')),'source_category':'未分类','book_title':''})
                result[key].update(bcc_document_id=row.get('bcc_document_id',''),file=row.get('file',''))
        if not self.workspace:
            scopes={r.get('source_name'):r for r in read_json(self.root/'index/scope_indexes.json').get('scopes',[])}
            for doc in result.values():
                scope=scopes.get(doc.get('file'))
                if scope:doc['bcc_scope']={k:scope[k] for k in ('start','end','title') if k in scope}
        return result

    def term_ids(self):
        if not self.terminology.is_file():return {}
        with self.terminology.open(encoding='utf-8-sig',newline='') as stream:
            return {r.get('preferred_term',''):r.get('term_id','') for r in csv.DictReader(stream) if r.get('preferred_term')}

    def iter_annotations(self,source_category='',book_title='',sentence_id='',term='',topic=''):
        docs=self.documents();ids=self.term_ids()
        from .topic_display import TopicDisplay
        display=TopicDisplay(self.root,self.workspace)
        topic_nodes=read_json(self.taxonomy).get('nodes',read_json(self.taxonomy).get('topics',[]))
        topic_ids={str(r.get('path') or r.get('full_path') or r.get('name','')):r.get('topic_id',r.get('id','')) for r in topic_nodes}
        for row in rows(self.annotations):
            doc_id=str(row.get('source_document_id',row.get('doc_id','')));doc=docs.get(doc_id,{'document_id':doc_id,'title':'','source_category':'未分类','book_title':''})
            sid=str(row.get('source_sentence_id',row.get('sentence_id',row.get('document_id',''))))
            if source_category and doc['source_category']!=source_category:continue
            if book_title and doc['book_title']!=book_title:continue
            if sentence_id and sid!=sentence_id:continue
            terms=list(dict.fromkeys(str(v) for v in row.get('terms',[]) if isinstance(v,str)))
            topics=list(dict.fromkeys(str(v) for v in row.get('topics',[]) if isinstance(v,str))) or ([row['topic_path']] if row.get('topic_path') else [])
            if term and term not in terms and term not in [ids.get(v) for v in terms]:continue
            if topic and not any(p==topic or p.startswith(topic+' > ') or topic_ids.get(p)==topic for p in topics):continue
            yield {'sentence_id':sid,'unit_id':row.get('document_id',''),'document_id':doc_id,'text':row.get('clean_text',row.get('text','')),'topics':[dict(display.resolve(p),path=p,topic_id=topic_ids.get(p,display.resolve(p)['topic_id'])) for p in topics],'terms':[{'term':t,'term_id':ids.get(t,'')} for t in terms],'source':doc,'provenance':row.get('provenance',{}),'analysis_unit_type':row.get('analysis_unit_type','sentence'),'annotation_status':row.get('final_decision',''),'mark_origin':'saved_annotations'}

    def summary(self,source_category='',book_title=''):
        with _SUMMARY_LOCK:
            return _summary(str(self.root),self.workspace,self.signature(),source_category,book_title)

@lru_cache(maxsize=4)
def _summary(root,workspace,signature,source_category,book_title):
    assets=KnowledgeAssets(Path(root),workspace);frequency=Counter();sentences=Counter();topics=Counter();n=0
    for row in assets.iter_annotations(source_category,book_title):
        n+=1
        for entry in row['terms']:
            t=entry['term'];frequency[t]+=row['text'].count(t);sentences[t]+=1
        for entry in row['topics']:topics[entry['path']]+=1
    ids=assets.term_ids()
    return {'available':assets.annotations.is_file(),'sentence_count':n,'terms':[{'term':t,'term_id':ids.get(t,''),'frequency':frequency[t],'sentence_frequency':sentences[t]} for t in sorted(sentences,key=lambda t:(-frequency[t],t))],'topic_counts':dict(topics),'frequency_definition':'已标注术语在其所属句子中的非重叠字面出现次数；sentence_frequency为包含该标注的句子/话轮数'}

def snapshot_knowledge(project_root:Path,destination:Path,document_ids=None):
    import shutil
    assets=KnowledgeAssets(project_root,True);destination.mkdir(parents=True,exist_ok=True)
    selected=set(document_ids) if document_ids is not None else None
    for source,name in [(assets.taxonomy,'taxonomy.json'),(assets.terminology,'terminology.csv')]:
        if source.is_file():shutil.copy2(source,destination/name)
    if assets.mapping.is_file():
        with assets.mapping.open(encoding='utf-8-sig',newline='') as incoming, (destination/'sources.csv').open('w',encoding='utf-8-sig',newline='') as out:
            writer=csv.DictWriter(out,fieldnames=['document_id','source_category','book_title']);writer.writeheader()
            for row in csv.DictReader(incoming):
                if selected is None or row.get('document_id') in selected:writer.writerow({k:row.get(k,'') for k in writer.fieldnames})
    with (destination/'documents.jsonl').open('w',encoding='utf-8') as stream:
        for doc_id,row in assets.documents().items():
            if selected is not None and doc_id not in selected:continue
            stream.write(json.dumps({'doc_id':doc_id,**row},ensure_ascii=False)+'\n')
    if assets.annotations.is_file():
        with (destination/'sentence_annotations.jsonl').open('w',encoding='utf-8') as stream:
            for row in rows(assets.annotations):
                if selected is not None and str(row.get('source_document_id',row.get('doc_id',''))) not in selected:continue
                stream.write(json.dumps(row,ensure_ascii=False)+'\n')
