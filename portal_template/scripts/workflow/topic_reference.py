"""Remove only the active reference; retain recoverable copies and old rounds."""
import json,shutil,uuid
from pathlib import Path
from datetime import datetime

def remove_reference(root: Path) -> dict:
    root=root.resolve();config_path=root/"configs/pipeline.json"
    config=json.loads(config_path.read_text(encoding="utf-8-sig"))
    value=config.get("topics",{}).get("reference_taxonomy")
    current=(root/value).resolve() if value else None
    if current and not current.is_relative_to((root/"configs").resolve()):
        raise ValueError("只能移除当前项目 configs 中的参考体系；不会修改原始语料")
    backup=root/"data/reference_archive"/(datetime.now().strftime("%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex[:8])
    backup.mkdir(parents=True)
    shutil.copy2(config_path,backup/"pipeline.json")
    if current and current.is_file():shutil.copy2(current,backup/"reference_taxonomy.json")
    generic=root/"configs/topics/reference_taxonomy.json"
    if generic.is_file() and generic!=current:shutil.copy2(generic,backup/"previous_generic_reference.json")
    generic.parent.mkdir(parents=True,exist_ok=True)
    generic.write_text("{}\n",encoding="utf-8")
    config.setdefault("topics",{})["reference_taxonomy"]="configs/topics/reference_taxonomy.json"
    temporary=config_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(config,ensure_ascii=False,indent=2)+"\n",encoding="utf-8");temporary.replace(config_path)
    if current and current!=generic and current.is_file():current.unlink()
    return {"ok":True,"archive_path":backup.relative_to(root).as_posix(),"message":"当前参考体系已移除并归档；新建轮次将从空体系开始，也可重新上传其他领域体系。历史轮次和已发布版本保留。"}


from corpus_topic_workflow.topic_taxonomy_store import flatten_published_taxonomy
