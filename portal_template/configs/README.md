# 配置目录

配置文件控制流程，不保存数据库密码或模型 API Key。

| 位置 | 作用 |
| --- | --- |
| `pipeline.json` | 项目版本、目录和加工阶段 |
| `cleaning.json` | 清洗规则 |
| `recognition.json` | 识别、验证、模型数量、聚合和决策规则 |
| `term_recognition/` | 本地术语基础识别配置 |
| `prompts/recognition/` | 正式版本化提示词 |
| `topics/new_energy_candidate_taxonomy.json` | 用户参考话题体系输入 |
| `topics/bertopic.json` | 可选聚类参数，不代表主页面固定使用 BERTopic |
| `collect/` | 网页采集配置 |
| `governance/` | 来源权利和覆盖目标 |

人工词表和分词资源放在 `inputs/`；密码放在本机 `.env`；自动结果放在 `data/`。
