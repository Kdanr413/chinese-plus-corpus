# terms

本目录保留旧命令行兼容入口：`extract.py` 抽取候选，`cluster_terms.py` 聚类并生成旧审核表，可选 `run_llm.py` 提供论文式模型证据，`publish_terms.py` 发布旧格式正式术语和 Jieba 保护词典。

从 Phase 3 开始，`extract.py` 的话题表、领域词表、已有术语表、参考词频、few-shot 和历史 LLM 结果均为可选输入；只提供 sections、sentences、配置和输出即可独立产生统计候选。传入全部旧参数时，CSV 字段与结果逻辑保持兼容。

新的可导入接口和 JSONL CLI 位于 `corpus_recognition.term`。注意：`cluster_terms.py` 和 `publish_terms.py` 仍属于旧审核/发布链路，当前继续要求正式 taxonomy；这个限制将在后续阶段按计划解除，不能把候选直接绕过审核发布。
