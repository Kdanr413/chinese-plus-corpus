# topics

话题流程固定为：`taxonomy_workflow.py` 生成 BERTopic 聚类和审核表 → 必选模型固定匹配 → `merge_mapping_evidence.py` 回填证据 → 人工审核 → `publish_taxonomy.py` 发布。`build_taxonomy_validation_task.py` 只用于可选的正式体系终检。参考体系来自 `configs/topics/new_energy_candidate_taxonomy.json`。
