"""Conservative source filtering through verified BCC scope indexes."""
from collections import Counter
from pathlib import Path
from .knowledge_assets import KnowledgeAssets,read_json,SOURCE_CATEGORIES
from .service import SearchRequestError

def selected_ranges(release_root,source_category,book_title):
    if source_category and source_category not in SOURCE_CATEGORIES:raise SearchRequestError('来源须为报刊、新闻、论文、教材或未分类')
    root=Path(release_root);assets=KnowledgeAssets(root);docs=list(assets.documents().values())
    wanted=[d for d in docs if (not source_category or d['source_category']==source_category) and (not book_title or d['book_title']==book_title)]
    if not wanted:return []
    manifest=read_json(root/'index/scope_indexes.json').get('scopes',[])
    if not manifest:raise SearchRequestError('此版本缺少经过验证的BCC来源范围索引，不能执行来源限制；请在服务器发布完整范围索引')
    by_file={str(r.get('source_name','')):r for r in manifest};ranges=[]
    for doc in wanted:
        scope=by_file.get(str(doc.get('file','')))
        if not scope:raise SearchRequestError('此版本来源目录与BCC范围索引不完整，已拒绝退化为全库检索')
        ranges.append((int(scope['start']),int(scope['end'])))
    return list(dict.fromkeys(ranges))

def scoped_query(engine,query,command,options,ranges):
    if command=='Freq':
        counts=Counter()
        for scope in ranges:
            result=engine.frequency(query,number=None,scope_ranges=[scope])
            counts.update({k:int(v) for k,v in result.get('Freq',{}).items()})
        limit=int(options.get('number') or len(counts));ordered=sorted(counts.items(),key=lambda r:(-r[1],r[0]))[:limit]
        return {'Freq':dict(ordered),'source_scope_count':len(ranges)}
    limit=int(options.get('number') or 20);start=int(options.get('page_no') or 0)*limit;total=0;contexts=[]
    for scope in ranges:
        first=engine.kwic(query,number=1,page_no=0,win_size=options.get('win_size',50),scope_ranges=[scope]);n=int(first.get('Total',len(first.get('Context',[]))));offset=max(0,start-total)
        if len(contexts)<limit and offset<n:
            page=offset//limit;skip=offset%limit
            while len(contexts)<limit and offset<n:
                data=engine.kwic(query,number=limit,page_no=page,win_size=options.get('win_size',50),scope_ranges=[scope]);rows=data.get('Context',[]);take=rows[skip:skip+limit-len(contexts)];contexts.extend(take)
                if not rows:break
                offset=(page+1)*limit;page+=1;skip=0
        total+=n
    return {'Context':contexts,'Total':total,'source_scope_count':len(ranges)}
