"""Source labels belong to metadata, never to the raw corpus contents."""
import csv,threading,uuid
from pathlib import Path
from scripts.workflow.atomic_files import replace_with_retry
_LOCK=threading.Lock()
CATEGORIES=("报刊","新闻","论文","政策文件","教材","未分类")

def validate_source(kind,book,require_book=True):
 if kind not in CATEGORIES:raise ValueError("来源须为报刊、新闻、论文、政策文件、教材或未分类")
 if len(book)>400:raise ValueError("教材书名不得超过400字符")
 if kind=="教材" and require_book and not book.strip():raise ValueError("教材必须填写具体书名")

def write_csv_atomic(path,fields,rows):
 path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
 try:
  with temp.open("w",encoding="utf-8-sig",newline="") as f:
   writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(rows)
  replace_with_retry(temp,path)
 finally:temp.unlink(missing_ok=True)

def upload_source_map(root):
 path=Path(root)/"inputs/metadata/upload_sources.csv"
 if not path.is_file():return {}
 with path.open(encoding="utf-8-sig",newline="") as f:return {r["path"]:r for r in csv.DictReader(f)}

def save_upload_sources(root,paths,kind,book):
 validate_source(kind,book,require_book=False);root=Path(root).resolve()
 with _LOCK:
  mapping=upload_source_map(root)
  for value in paths:
   path=(root/value).resolve()
   if not path.is_relative_to(root/"data/raw"):raise ValueError("来源登记路径必须属于当前项目原始语料")
   title=(book.strip() or path.stem) if kind=="教材" else ""
   validate_source(kind,title)
   relative=path.relative_to(root).as_posix();mapping[relative]={"path":relative,"source_category":kind,"book_title":title}
  write_csv_atomic(root/"inputs/metadata/upload_sources.csv",["path","source_category","book_title"],mapping.values())
