"""Resolve saved topic identifiers/paths to names without guessing from keywords."""
import csv,json,re
from pathlib import Path

class TopicDisplay:
 def __init__(self,root,workspace=False):
  root=Path(root);base=root/"data/curated/topics" if workspace else root/"metadata/knowledge"
  self.taxonomy=json.loads((base/"taxonomy.json").read_text(encoding="utf-8-sig")) if (base/"taxonomy.json").is_file() else {}
  self.nodes=[];self.aliases={}
  for original in self.taxonomy.get("nodes",self.taxonomy.get("topics",[])):
   row=dict(original);tid=str(row.get("topic_id") or row.get("id") or "");path=str(row.get("path") or row.get("full_path") or row.get("name") or "")
   raw=str(row.get("name") or path.split(" > ")[-1]);match=re.match(r"^(BT_TOPIC_\d+)\s*[｜|]",raw) or re.search(r"待确认聚类 (BT_TOPIC_\d+)",raw)
   name="待命名聚类 "+match[1] if match and not raw.startswith("待确认聚类 ") else raw
   words=[]
   for field in ('keywords','representative_keywords'):
    values=row.get(field) or []
    if isinstance(values,str):values=[values]
    for word in values:
     if isinstance(word,str) and word.strip() and word.strip() not in words:words.append(word.strip())
   # Older test exports saved representative words in the original cluster label.
   if not words and re.match(r"^BT_TOPIC_\d+\s*[｜|]",raw):
    words=[word.strip() for word in re.split(r"[、,，]",re.split(r"[｜|]",raw,maxsplit=1)[1]) if word.strip()]
   row.update(topic_id=tid,original_name=raw,display_name=name,name=name,path=path,needs_name=bool(match),level=len([part for part in path.split('>') if part.strip()]),keywords=words,keywords_missing=not bool(words))
   self.nodes.append(row)
   for alias in {tid,path,raw,path.split(" > ")[-1],re.sub(r"^BT_TOPIC_\d+\s*[｜|]\s*","",raw)}:
    if alias:self.aliases.setdefault(alias,[]).append(row)
 def resolve(self,path):
  matches=self.aliases.get(str(path),[])
  if len(matches)!=1:return {"topic_id":"","path":str(path),"name":"未匹配到话题名称","needs_name":True}
  row=matches[0]
  return {k:row[k] for k in ("topic_id","path","name","needs_name")}
