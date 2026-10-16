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


def flatten_published_taxonomy(reference: dict) -> list[dict]:
    """Validate an exported taxonomy, retaining IDs and all top-level branches."""
    rows = reference.get("nodes")
    if not isinstance(rows, list) or not rows:
        raise ValueError("正式话题体系的 nodes 必须为非空节点列表")
    nodes, ids, paths = [], {}, set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("话题节点必须为对象")
        node_id = str(row.get("id", "")).strip()
        name = str(row.get("name", "")).strip()
        parts = [part.strip() for part in str(row.get("path", "")).split(">")]
        if not node_id or not name or not all(parts) or len(parts)>3 or parts[-1]!=name:
            raise ValueError("话题节点缺少 id/name/path，或路径层级、末级名称不正确")
        path = " > ".join(parts)
        if node_id in ids or path in paths:
            raise ValueError("话题体系包含重复节点 ID 或路径")
        keywords = row.get("keywords", [])
        if not isinstance(keywords, list) or any(not isinstance(word,str) for word in keywords):
            raise ValueError("话题关键词 keywords 必须为字符串列表")
        node = dict(row, id=node_id, name=name, path=path,
                    parent_id=str(row.get("parent_id") or "").strip(),
                    keywords=keywords, depth=len(parts)-1,
                    node_type="root" if len(parts)==1 else "main_tree",
                    assignable=len(parts)==3)
        nodes.append(node); ids[node_id]=node; paths.add(path)
    for node in nodes:
        parent = ids.get(node["parent_id"])
        expected = " > ".join(node["path"].split(" > ")[:-1])
        if (expected and (not parent or parent["path"]!=expected)) or (not expected and node["parent_id"]):
            raise ValueError("父节点缺失或与完整路径不一致："+node["path"])
    return [node for node in nodes if node.get("status","active")=="active"]
