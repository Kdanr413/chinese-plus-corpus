# corpus_recognition

负责全量话题标注、术语识别、验证、可选交叉检验和综合决策。

## 输入

- 清洗后的完整句子或完整对话话轮；
- `data/curated/topics/taxonomy.json` 正式话题体系；
- `inputs/linguistic/` 和 `configs/term_recognition/` 的术语参考；
- `configs/recognition.json` 与 `configs/prompts/recognition/`；
- 页面临时提供的外部模型配置（可选）。

## 输出

每次任务写入 `data/workspaces/chinese-plus/recognition_*/recognition/`，依次保留预处理、术语候选、术语验证、话题候选、话题验证、交叉检验、决策和最终逐句资源。激活后写入 `data/curated/`。

## 当前规则

- 记叙文按完整句子处理，对话体按完整说话人话轮处理；
- 话题使用已经发布的三级体系做全量分类；
- 术语先运行 `term_engine.py` 的基础识别；
- 启用外部模型时，由 `term_intersection.py` 逐句调用模型并与基础候选求交集；
- 交叉检验可关闭；启用时只接受正式体系内的修正路径；
- 所有候选、模型意见、理由和决策保留 provenance；
- PostgreSQL 只保存可查询元数据，正文和原始证据仍在 Workspace。
