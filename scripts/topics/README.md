# topics

话题流程固定为：`taxonomy_workflow.py` 生成 BERTopic 聚类和审核表 → 必选模型固定匹配 → `merge_mapping_evidence.py` 回填证据 → 人工审核 → `publish_taxonomy.py` 发布。`build_taxonomy_validation_task.py` 只用于可选的正式体系终检。参考体系以 `configs/pipeline.json` 的 topics.reference_taxonomy 为准；新发行包使用空的 `configs/topics/reference_taxonomy.json`，新能源文件仅为可选示例。
