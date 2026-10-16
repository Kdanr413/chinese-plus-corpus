from __future__ import annotations

import json
import re
import copy
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "1.0"
CHANGE_LOG_SCHEMA_VERSION = "1.0"
PATH_SEPARATOR = " > "
BOUNDARY_PATH_MIGRATIONS = {
    "常规水电与抽水蓄能": "新能源边界领域 > 水能与传统储能 > 常规水电与抽水蓄能",
    "核能": "新能源边界领域 > 非化石能源 > 核能",
    "新能源汽车": "新能源边界领域 > 交通能源应用 > 新能源汽车",
}
PARENT_DEFINITIONS = {
    "新能源": "围绕太阳能、风能、生物质能、地热能、海洋能、氢能、新型储能及其接入与协同利用形成的技术与系统领域。",
    "新能源边界领域": "与新能源语料密切相关，但是否纳入新能源主树存在领域口径差异的相邻能源与应用领域。",
    "新能源边界领域 > 水能与传统储能": "水力发电以及以抽水方式实现电力调节和储能的技术与系统类别。",
    "新能源边界领域 > 非化石能源": "不直接依赖化石燃料、但与新能源的政策和学科口径需要区分的能源类别。",
    "新能源边界领域 > 交通能源应用": "新能源载体、动力系统及补能设施在交通领域中的应用类别。",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def split_topic_path(value: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"\s*(?:>|＞|→|/)\s*", str(value or "")) if part.strip()]
    if len(parts) > 3:
        raise ValueError("话题路径最多支持一级、二级、三级三个层级")
    return parts


def normalize_topic_path(value: str) -> str:
    return PATH_SEPARATOR.join(split_topic_path(value))


def topic_record(
    path: str, *, source: str, created_by: str,
    definition: str = "", keywords: Iterable[str] = (), status: str = "active",
    scope_status: str = "IN_SCOPE",
) -> dict[str, Any]:
    parts = split_topic_path(path)
    if not parts:
        raise ValueError("话题路径不能为空")
    normalized = PATH_SEPARATOR.join(parts)
    return {
        "path": normalized,
        "level": len(parts),
        "level_1": parts[0],
        "level_2": parts[1] if len(parts) > 1 else "",
        "level_3": parts[2] if len(parts) > 2 else "",
        "name": parts[-1],
        "parent_path": PATH_SEPARATOR.join(parts[:-1]),
        "definition": str(definition or "").strip(),
        "keywords": sorted({str(value).strip() for value in keywords if str(value).strip()}),
        "status": status,
        "scope_status": "OUT_OF_SCOPE" if scope_status == "OUT_OF_SCOPE" else "IN_SCOPE",
        "source": source,
        "created_by": created_by,
        "created_at": now_iso(),
    }


def empty_taxonomy(name: str = "动态话题体系") -> dict[str, Any]:
    timestamp = now_iso()
    return {
        "schema_version": SCHEMA_VERSION,
        "taxonomy_name": name,
        "taxonomy_version": 1,
        "updated_at": timestamp,
        "path_separator": PATH_SEPARATOR,
        "max_levels": 3,
        "topics": [],
    }


def read_taxonomy(path: Path) -> dict[str, Any]:
    if not path.exists() or path.stat().st_size == 0:
        return empty_taxonomy()
    state = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(state.get("topics"), list):
        raise ValueError(f"动态话题体系格式错误，topics 必须为数组：{path}")
    return state


def taxonomy_changes_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}_changes.json")


def read_taxonomy_changes(path: Path) -> dict[str, Any]:
    changes_path = taxonomy_changes_path(path)
    if not changes_path.exists() or changes_path.stat().st_size == 0:
        return {
            "schema_version": CHANGE_LOG_SCHEMA_VERSION,
            "taxonomy_file": path.name,
            "current_taxonomy_version": 0,
            "updated_at": "",
            "changes": [],
        }
    payload = json.loads(changes_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload.get("changes"), list):
        raise ValueError(f"话题体系维护记录格式错误，changes 必须为数组：{changes_path}")
    return payload


def append_taxonomy_changes(path: Path, changes: Iterable[dict[str, Any]], state: dict[str, Any]) -> None:
    if path.name != "dynamic_topic_taxonomy.json":
        return
    incoming = [copy.deepcopy(row) for row in changes if isinstance(row, dict)]
    log = read_taxonomy_changes(path)
    combined = [*log.get("changes", []), *incoming]
    deduplicated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in combined:
        identity = json.dumps(row, ensure_ascii=False, sort_keys=True)
        if identity in seen:
            continue
        seen.add(identity)
        deduplicated.append(row)
    log.update({
        "schema_version": CHANGE_LOG_SCHEMA_VERSION,
        "taxonomy_file": path.name,
        "current_taxonomy_version": int(state.get("taxonomy_version", 0)),
        "updated_at": str(state.get("updated_at") or now_iso()),
        "changes": deduplicated,
    })
    changes_path = taxonomy_changes_path(path)
    changes_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = changes_path.with_suffix(changes_path.suffix + ".tmp")
    temporary.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(changes_path)


def write_taxonomy(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = copy.deepcopy(state)
    embedded_changes = payload.pop("change_log", [])
    append_taxonomy_changes(path, embedded_changes, payload)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def normalize_three_level_taxonomy(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Normalize a flat node table while retaining level-1/2 categories as judgment labels."""
    state = copy.deepcopy(state)
    changed = False
    for row in state.get("topics", []):
        old_path = normalize_topic_path(row.get("path", ""))
        new_path = BOUNDARY_PATH_MIGRATIONS.get(old_path, old_path)
        if new_path != old_path:
            row["path"] = new_path
            changed = True

    existing = {
        normalize_topic_path(row.get("path", ""))
        for row in state.get("topics", []) if normalize_topic_path(row.get("path", ""))
    }
    for path in list(existing):
        parts = split_topic_path(path)
        for index in range(1, len(parts)):
            parent_path = PATH_SEPARATOR.join(parts[:index])
            if parent_path in existing:
                continue
            state["topics"].append(topic_record(
                parent_path, source="generated_parent", created_by="hierarchy_normalizer",
            ))
            existing.add(parent_path)
            changed = True

    for row in state.get("topics", []):
        path = normalize_topic_path(row.get("path", ""))
        parts = split_topic_path(path)
        expected = {
            "path": path, "level": len(parts),
            "level_1": parts[0] if parts else "",
            "level_2": parts[1] if len(parts) > 1 else "",
            "level_3": parts[2] if len(parts) > 2 else "",
            "name": parts[-1] if parts else "",
            "parent_path": PATH_SEPARATOR.join(parts[:-1]),
        }
        for key, value in expected.items():
            if row.get(key) != value:
                row[key] = value
                changed = True
        if row.get("scope_status") not in {"IN_SCOPE", "OUT_OF_SCOPE"}:
            allowed_roots = set((state.get("scope_policy") or {}).get("allowed_level_1_topics", []))
            row["scope_status"] = (
                "IN_SCOPE" if not allowed_roots or (parts and parts[0] in allowed_roots)
                else "OUT_OF_SCOPE"
            )
            changed = True
        if len(parts) < 3 and not str(row.get("definition", "")).strip() and path in PARENT_DEFINITIONS:
            row["definition"] = PARENT_DEFINITIONS[path]
            changed = True
        if len(parts) < 3 and row.get("keywords"):
            row["keywords"] = []
            changed = True

    state["max_levels"] = 3
    state["hierarchy_policy"] = {
        "classification_order": ["level_1", "level_2", "level_3"],
        "complete_assignment_required": True,
        "level_3_prefer_existing": True,
        "definition_required_levels": [3],
        "keywords_allowed_levels": [3],
    }
    missing_definitions = sorted(
        row["path"] for row in state.get("topics", [])
        if row.get("status", "active") == "active" and int(row.get("level", 0)) == 3
        and not str(row.get("definition", "")).strip()
    )
    state["taxonomy_validation"] = {
        "valid": not missing_definitions,
        "missing_level_3_definitions": missing_definitions,
    }
    if changed:
        state["taxonomy_version"] = int(state.get("taxonomy_version", 1)) + 1
        state["updated_at"] = now_iso()
        state.setdefault("change_log", []).append({
            "taxonomy_version": state["taxonomy_version"],
            "changed_at": state["updated_at"],
            "created_by": "hierarchy_normalizer",
            "source": "taxonomy_maintenance",
            "reason": "统一三级联合标注结构；保留一级二级判断层，聚类词仅保留在三级话题",
        })
    return state, changed


def active_paths(state: dict[str, Any]) -> list[str]:
    return sorted({
        normalize_topic_path(row.get("path", ""))
        for row in state.get("topics", [])
        if row.get("status", "active") == "active" and normalize_topic_path(row.get("path", ""))
    }, key=lambda value: (len(split_topic_path(value)), value))


def assignable_paths(state: dict[str, Any]) -> list[str]:
    """Return active terminal labels. Categories remain in the tree but cannot label text."""
    paths = active_paths(state)
    path_set = set(paths)
    return sorted(
        path for path in paths
        if len(split_topic_path(path)) == 3
        and not any(other.startswith(path + PATH_SEPARATOR) for other in path_set)
    )


def active_topics(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return active topic metadata in stable hierarchy order."""
    allowed = set(assignable_paths(state))
    rows = [
        {
            "path": normalize_topic_path(row.get("path", "")),
            "level": int(row.get("level") or len(split_topic_path(row.get("path", "")))),
            "name": str(row.get("name") or "").strip(),
            "parent_path": normalize_topic_path(row.get("parent_path", "")),
            "definition": str(row.get("definition") or "").strip(),
            "keywords": sorted({str(value).strip() for value in row.get("keywords", []) if str(value).strip()}),
            "scope_status": str(row.get("scope_status") or "IN_SCOPE"),
            "assignable": normalize_topic_path(row.get("path", "")) in allowed,
            "node_role": "leaf_topic" if normalize_topic_path(row.get("path", "")) in allowed else "category",
        }
        for row in state.get("topics", [])
        if row.get("status", "active") == "active" and normalize_topic_path(row.get("path", ""))
    ]
    return sorted(rows, key=lambda row: (row["level"], row["path"]))


def add_topic_path(
    state: dict[str, Any], path: str, *, source: str, created_by: str,
    definition: str = "", keywords: Iterable[str] = (), change_reason: str = "新增话题",
    scope_status: str = "IN_SCOPE",
) -> list[str]:
    parts = split_topic_path(path)
    if not parts:
        raise ValueError("新增话题路径不能为空")
    existing = {normalize_topic_path(row.get("path", "")) for row in state.get("topics", [])}
    added: list[str] = []
    for index in range(1, len(parts) + 1):
        current = PATH_SEPARATOR.join(parts[:index])
        if current in existing:
            continue
        is_leaf = index == len(parts)
        state["topics"].append(topic_record(
            current,
            source=source if is_leaf else "generated_parent",
            created_by=created_by,
            definition=definition if is_leaf else "",
            keywords=keywords if is_leaf else (),
            scope_status=scope_status,
        ))
        existing.add(current)
        added.append(current)
    if added:
        state["taxonomy_version"] = int(state.get("taxonomy_version", 0)) + 1
        state["updated_at"] = now_iso()
        state.setdefault("change_log", []).append({
            "taxonomy_version": state["taxonomy_version"],
            "changed_at": state["updated_at"],
            "created_by": created_by,
            "source": source,
            "reason": change_reason,
            "added_paths": added,
        })
    return added


def initialize_or_merge_taxonomy(
    path: Path, nodes: list[dict[str, Any]], *, taxonomy_name: str = "动态话题体系",
) -> dict[str, Any]:
    existed = path.exists() and path.stat().st_size > 0
    state = read_taxonomy(path) if existed else empty_taxonomy(taxonomy_name)
    if not state.get("taxonomy_name"):
        state["taxonomy_name"] = taxonomy_name
    changed = False
    existing = {normalize_topic_path(row.get("path", "")) for row in state.get("topics", [])}
    for node in nodes:
        if node.get("node_type") not in {"root", "main_tree", "boundary"}:
            continue
        normalized = normalize_topic_path(node.get("path", ""))
        if not normalized or normalized in existing:
            continue
        record = topic_record(
            normalized, source="reference_taxonomy", created_by="system_seed",
            definition=node.get("definition", ""), keywords=node.get("keywords", ()),
        )
        state["topics"].append(record)
        existing.add(normalized)
        changed = True
    if changed and existed:
        state["taxonomy_version"] = int(state.get("taxonomy_version", 0)) + 1
        state["updated_at"] = now_iso()
        state.setdefault("change_log", []).append({
            "taxonomy_version": state["taxonomy_version"],
            "changed_at": state["updated_at"],
            "created_by": "system_seed",
            "source": "reference_taxonomy",
            "reason": "同步参考体系中的新增节点",
        })
    write_taxonomy(path, state)
    return state


def taxonomy_tree(state: dict[str, Any]) -> list[dict[str, Any]]:
    roots: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    metadata = {row["path"]: row for row in active_topics(state)}
    for path in active_paths(state):
        parts = split_topic_path(path)
        current_path = ""
        parent_children = roots
        for part in parts:
            current_path = PATH_SEPARATOR.join(filter(None, (current_path, part)))
            node = index.get(current_path)
            if node is None:
                details = metadata.get(current_path, {})
                node = {
                    "name": part,
                    "path": current_path,
                    "level": len(split_topic_path(current_path)),
                    "definition": details.get("definition", ""),
                    "keywords": details.get("keywords", []),
                    "children": [],
                }
                index[current_path] = node
                parent_children.append(node)
            parent_children = node["children"]
    return roots


def upsert_jsonl(path: Path, record: dict[str, Any], key_fields: tuple[str, ...]) -> None:
    rows: list[dict[str, Any]] = []
    if path.exists():
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    key = tuple(str(record.get(field, "")) for field in key_fields)
    replaced = False
    for index, row in enumerate(rows):
        if tuple(str(row.get(field, "")) for field in key_fields) == key:
            rows[index] = record
            replaced = True
            break
    if not replaced:
        rows.append(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    temporary.replace(path)


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
