# prepare

| 文件 | 作用 |
| --- | --- |
| `inventory.py` | 扫描本轮原始文件、去重、质量预判和范围统计 |
| `clean.py` | 正文清洗、标题层级、文体识别和四级 JSONL 输出 |
| `llm_clean_review.py` | 可选模型清洗任务、回复审计与安全应用 |

清洗规则应放在 `configs/cleaning.json`，不要把项目特定阈值硬编码到脚本。
