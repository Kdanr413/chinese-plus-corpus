# 上传模板与格式

这里存放可复制填写的模板和示例，不是程序运行输入目录。填写后请从网页操作台上传。

| 上传内容 | 推荐模板 | 上传后当前生效位置 |
| --- | --- | --- |
| 参考话题体系 | `../configs/topics/new_energy_candidate_taxonomy.json` | `configs/topics/new_energy_candidate_taxonomy.json` |
| 领域词表 | `模板/领域词表.csv`，也支持一行一词 TXT | `inputs/linguistic/domain_lexicon.csv` |
| 已有术语 | `模板/规范术语表.csv`，也支持一行一词 TXT | `inputs/linguistic/terminology.csv` |
| 分词规则 | `模板/分词规则表.csv` | `inputs/linguistic/segmentation_notes.csv` |
| Jieba 用户词典 | `模板/Jieba用户词典.txt` | `inputs/linguistic/jieba_user_dict.txt` |
| 排除术语 | `模板/排除术语表.txt` | `inputs/linguistic/stop_terms.txt` |
| 典型领域文件 | `模板/典型领域文件登记.csv` | `inputs/domain/reference_documents.csv` |
| 术语示例 | `模板/术语抽取示例.jsonl` | `inputs/term_extraction/few_shot_examples.jsonl` |

## 填写规则

1. CSV 和 JSON/JSONL 使用 UTF-8；标准模板的字段名不要修改。
2. 简单词表可每行写一个词，平台会补齐规范字段并去重。
3. 多个别名使用半角竖线 `|` 分隔。
4. `status=active` 表示启用，`inactive` 表示保留但本轮不用。
6. 上传原件会保存在 `data/raw/reference_uploads/`，不会只保留最后一个文件。

