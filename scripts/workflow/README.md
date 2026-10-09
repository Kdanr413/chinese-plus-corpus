# workflow 底层实现

| 文件 | 作用 | 主要输出 |
| --- | --- | --- |
| `core.py` | 配置、路径、哈希和通用执行 | 阶段状态与报告 |
| `prompting.py` | 提示词渲染和响应格式约束 | 模型请求内容 |
| `run_validation_llm.py` | 兼容模型调用和回复留档 | 模型响应审计 |
| `stages.py` | 清洗后语言加工、结构化、句法树和发布阶段 | `data/processed/` |
| `structure_reuse.py` | 比较语料、话题、术语、分词和结构化规则 checksum | 复用或重建决定 |

当前话题体系弹窗由 `corpus_topic_workflow/` 提供；全量识别和逐句标签由 `corpus_recognition/` 提供。
