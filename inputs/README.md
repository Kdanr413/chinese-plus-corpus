# 人工参考输入

这里保存当前生效的人工参考资源，自动结果不会反向写入本目录。

| 位置 | 内容 | 使用阶段 |
| --- | --- | --- |
| `linguistic/` | 领域词表、已有术语、Jieba 词典、分词备注和排除词 | 术语识别与分词 |
| `domain/reference_documents.csv` | 典型领域文件登记 | 话题和术语参考 |
| `term_extraction/few_shot_examples.jsonl` | 术语识别示例 | 外部模型提示参考 |
| `method_references/` | 方法论文及登记 | 方法研究，不直接作为语料 |

页面上传原件保存在 `data/raw/reference_uploads/`，规范化和去重后的当前版本写入这里。格式模板见 `../参考文件中心/README.md`。
