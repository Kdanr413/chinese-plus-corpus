"""Shared prompt configuration for production LLM calls and the UI test workbench."""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_PROMPTS: dict[str, dict[str, Any]] = {
    "cleaning_validation": {
        "name": "清洗文本语言形式复核",
        "description": "可选。逐段检查异常断句、标点、OCR 空格和有明确依据的少量 OCR 错别字；原始文件始终不变。",
        "system_prompt": "你是中文 OCR 语料清洗助手。你只修复输入 original_text 中的格式错误、异常空格、错位标点、句界错误，以及由上下文能够唯一确认的少量 OCR 错别字。previous_context 与 next_context 仅用于判断，不得修改。原始文件由系统另行永久保存；你返回的 corrected_text 只能作为清洗副本。不得润色、改写、概括或补写内容，不得增删实词，不得改变数字、专名、术语和事实，不得合并或拆分段落。不确定时必须选择 needs_human_review。严格输出指定 JSON。",
        "task_instruction": "逐字符核对 original_text：1. 无问题时 decision=keep；2. 格式错误或上下文能够唯一确认的 OCR 错别字时 decision=correct，并返回完整 corrected_text；3. 每个错别字必须在 changes 中以 type=ocr_typo 逐字记录 before、after 和判断依据；4. 疑似漏字、多字、事实错误或无法唯一确认时 decision=needs_human_review。不得借清洗之名改写原句。",
        "output_schema": "{\"task_type\":\"cleaning_text_review\",\"paragraph_id\":\"\",\"decision\":\"keep|correct|needs_human_review\",\"corrected_text\":null,\"issue_types\":[],\"changes\":[{\"type\":\"word_spacing|whitespace|punctuation|sentence_boundary|ocr_artifact|ocr_typo\",\"before\":\"\",\"after\":\"\",\"reason\":\"\"}],\"confidence\":0.0,\"review_warnings\":[]}。correct 时 corrected_text 必须为完整段落；OCR 错别字必须逐字声明；keep/needs_human_review 时 corrected_text 必须为 null。",
        "sample_payload": {"task_type": "cleaning_text_review", "paragraph_id": "TEST_P1", "previous_context": "电池管理系统实时采集电芯状态。", "original_text": "系统通过传感嚣采集温度。", "next_context": "传感器数据用于热管理控制。", "allowed_changes": ["格式错误", "有明确上下文依据的少量 OCR 错别字"], "forbidden_changes": ["润色或改写", "增删实词", "改变数字、专名和术语"]},
    },
    "topic_mapping": {
        "name": "BERTopic 聚类结果与参考体系固定匹配",
        "description": "必选。逐个聚类匹配固定参考体系；匹配不到时形成新增话题候选。",
        "system_prompt": "你是新能源领域话题体系匹配助手。本任务位于 BERTopic 聚类之后、人工审核之前。输入固定为一个聚类结果和一份人工参考话题体系。你只能执行一个确定流程：先在参考体系中寻找最合适的已有节点；若有匹配，输出该已有节点；若没有匹配，输出新增话题候选。不得把一个已有话题和新增话题同时作为主结果，不得虚构节点、文本、统计值或证据 ID，不得生成正式节点 ID，不得修改参考体系。只能依据任务提供的真实证据，严格输出指定 JSON，不输出 Markdown、前言或思维过程。",
        "task_instruction": "按照固定顺序处理：1. 阅读聚类代表词、中心/随机/边界样本和统计信息；2. 对照 reference_nodes 的名称、完整路径、定义、关键词、纳入和排除边界；3. 若已有节点能够覆盖聚类的主要语义，resolution 必须为 match_existing，并只输出一个 matched_node；4. 若所有已有节点都不能覆盖，resolution 必须为 propose_new，并填写新增话题名称、定义、建议父节点、关键词及纳入/排除范围；5. 引用证据单元 ID并说明理由。partial 仍属于 match_existing，并在 review_warnings 中说明边界问题。",
        "output_schema": "{\"task_type\":\"cluster_to_taxonomy_mapping\",\"cluster_id\":\"\",\"resolution\":\"match_existing|propose_new\",\"matched_node\":{\"node_id\":\"\",\"node_name\":\"\",\"confidence\":0.0,\"evidence_unit_ids\":[],\"reason\":\"\"},\"new_topic_proposal\":null,\"cluster_nature\":\"main_topic|cross_cutting|boundary|mixed|noise|insufficient\",\"review_warnings\":[]}。resolution 为 match_existing 时 new_topic_proposal 必须为 null；resolution 为 propose_new 时 matched_node 必须为 null，new_topic_proposal 为 {\"name\":\"\",\"definition\":\"\",\"suggested_parent_id\":null,\"keywords\":[],\"include_scope\":\"\",\"exclude_scope\":\"\"}。",
        "sample_payload": {"task_type": "cluster_to_taxonomy_mapping", "cluster": {"cluster_id": "BT_TOPIC_TEST", "representative_words": [{"term": "动力电池", "score": 0.72}], "samples": [{"unit_id": "TEST_U1", "sample_type": "center", "excerpt": "动力电池系统包括电芯、模组和电池管理系统。"}]}, "reference_nodes": [{"id": "NE_BATTERY", "name": "动力电池", "definition": "新能源汽车动力电池技术及系统。", "path": "新能源/动力电池"}]},
    },
    "taxonomy_validation": {
        "name": "正式话题体系结构核验",
        "description": "可选。人工发布后检查名称、层级、边界、重复和覆盖缺口。",
        "system_prompt": "你是新能源领域话题体系质量审核助手。只依据任务中已经人工发布的正式体系进行结构核验。不得直接修改体系，不得创造节点或证据，只报告可复核的问题与建议。只输出 JSON，不输出 Markdown、前言或思维过程。",
        "task_instruction": "逐项核验名称一致性、父子层级、重复或交叉节点、定义边界、孤立节点和覆盖缺口。每个问题必须指明现有节点 ID、严重程度、依据和建议；没有发现问题时返回空 issues。",
        "output_schema": "{\"task_type\":\"final_taxonomy_structure_validation\",\"overall_status\":\"pass|needs_revision|insufficient\",\"issues\":[{\"issue_type\":\"duplicate|overlap|hierarchy|definition|boundary|coverage|orphan\",\"severity\":\"high|medium|low\",\"node_ids\":[],\"evidence\":\"\",\"suggestion\":\"\"}],\"summary\":\"\"}",
        "sample_payload": {"task_type": "final_taxonomy_structure_validation", "taxonomy": {"status": "approved", "nodes": [{"id": "ROOT", "name": "新能源", "parent_id": ""}, {"id": "NE_BATTERY", "name": "动力电池", "parent_id": "ROOT", "definition": "动力电池技术及系统。"}]}},
    },
    "term_validation": {
        "name": "领域术语候选验证",
        "description": "可选。判断统计抽取候选的专业性、规范形式、语义和覆盖情况。",
        "system_prompt": "你是新能源领域术语审核助手。只能依据候选术语、真实上下文、同簇候选和正式话题体系进行判断。不得把模型常识中的新词直接加入结果，不得虚构文本、术语 ID、话题 ID 或证据。只输出 JSON，不输出 Markdown、前言或思维过程。",
        "task_instruction": "判断候选是否是在当前语料中具有稳定专业概念的新能源术语，并检查规范形式、同义或变体关系、歧义、正式话题归属及上下文是否充分。只能选择 allowed_actions 中的动作。全面性只针对任务提供的上下文：列出上下文中明显遗漏、且逐字出现的术语建议；这些建议仍须进入人工审核。",
        "output_schema": "{\"candidate_id\":\"\",\"candidate_term\":\"\",\"decision\":\"accept|reject|normalize|merge|split_sense|needs_context\",\"is_domain_term\":true,\"preferred_term\":null,\"merge_target_candidate_id\":null,\"topic_node_ids\":[],\"evidence_excerpts\":[],\"reason\":\"\",\"possible_omissions\":[{\"term\":\"\",\"source_excerpt\":\"\"}],\"confidence\":0.0,\"review_warnings\":[]}",
        "sample_payload": {"candidate_id": "TERM_TEST_001", "candidate_term": "荷电状态", "contexts": "电池管理系统根据电芯电压估算荷电状态。", "cluster_neighbors": "电池管理系统|电芯电压", "allowed_actions": ["accept", "reject", "normalize", "merge", "split_sense", "needs_context"]},
    },
}


def validate_prompt_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("提示词配置必须是 JSON 对象")
    supplied = value.get("prompts", value)
    if not isinstance(supplied, dict):
        raise ValueError("prompts 必须是对象")
    prompts: dict[str, dict[str, Any]] = {}
    for key, default in DEFAULT_PROMPTS.items():
        item = supplied.get(key, default)
        if not isinstance(item, dict):
            raise ValueError(f"提示词模板格式错误：{key}")
        clean = {"name": str(item.get("name") or default["name"]).strip(), "description": str(item.get("description") or default["description"]).strip()}
        for field in ("system_prompt", "task_instruction", "output_schema"):
            text = str(item.get(field, "")).strip()
            if not text:
                raise ValueError(f"{key}.{field} 不能为空")
            if len(text) > 50000:
                raise ValueError(f"{key}.{field} 超过 50,000 字符")
            clean[field] = text
        sample = item.get("sample_payload", default["sample_payload"])
        if not isinstance(sample, dict):
            raise ValueError(f"{key}.sample_payload 必须是 JSON 对象")
        clean["sample_payload"] = sample
        prompts[key] = clean
    return {"schema_version": "1.0", "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "prompts": prompts}


def load_prompt_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": "1.0", "updated_at": "", "prompts": copy.deepcopy(DEFAULT_PROMPTS)}
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        validated = validate_prompt_config(raw)
        # Migrate the pre-1.1 bidirectional template to the fixed one-way matcher.
        old_topic = validated["prompts"].get("topic_mapping", {})
        if "cluster_to_taxonomy_mapping" not in str(old_topic.get("output_schema", "")):
            validated["prompts"]["topic_mapping"] = copy.deepcopy(DEFAULT_PROMPTS["topic_mapping"])
        validated["updated_at"] = str(raw.get("updated_at", ""))
        return validated
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"提示词配置无效：{exc}") from exc


def save_prompt_config(path: Path, value: Any) -> dict[str, Any]:
    config = validate_prompt_config(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.with_suffix(path.suffix + ".previous").write_bytes(path.read_bytes())
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return config


def prompt_key_for_task(task: dict[str, Any], mode: str) -> str:
    if mode == "cleaning":
        return "cleaning_validation"
    if mode == "terms":
        return "term_validation"
    return "taxonomy_validation" if task.get("task_type") == "final_taxonomy_structure_validation" else "topic_mapping"


def build_messages(template: dict[str, Any], task: dict[str, Any]) -> list[dict[str, str]]:
    user = f"任务说明：\n{template['task_instruction']}\n\n任务数据：\n{json.dumps(task, ensure_ascii=False)}\n\n必须遵守的输出结构：\n{template['output_schema']}"
    return [{"role": "system", "content": str(template["system_prompt"])}, {"role": "user", "content": user}]


def parse_json_content(content: str) -> Any:
    cleaned = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(cleaned)
