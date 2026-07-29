# 新能源及新能源汽车语料库处理说明

本项目维护 `D:\production\corpus` 下新能源及新能源汽车语料的清洗、规范化、层级切分和统计流程。

## 当前处理进度

更新时间：2026-07-28

已完成：

- 对 `D:\production\corpus\book` 与 `D:\production\corpus\crawler` 的可用语料进行去重和质量筛查。
- 将可用文本清洗为统一 UTF-8 文本，剔除网页导航/页脚、栏目索引、练习题块、重复页眉、过短内容和清洗后重复内容。
- 针对爬虫文本提取 `TITLE/URL/DATE` 元数据和正文边界，并过滤主页、列表页及领域相关性不足的网页。
- 收紧标题识别规则，排除日期时间、表格数字、目录点线、长法律条款和正文式编号句；保留识别规则与置信度供审计。
- 保留文档级、章节/标题级、段落级结构。
- 新增句子级切分，并在文档统计中写入句子数量。
- 按来源类型和来源站点/来源集合，将清洗后的文档 TXT 分目录保存。
- 建立“主分类树＋知识维度标签”的领域话题种子体系。
- 新增基于规则、TF-IDF 和球面 K-means 的章节话题发现流程。
- 生成章节级候选标签、候选话题体系、术语表和人工核验队列。
- 提供可选的大模型聚类命名、层级建议、拆分与合并建议功能。

## 核心脚本

主处理脚本：

```powershell
D:\ProgramData\anaconda3\python.exe D:\production\corpus\scripts\build_clean_corpus_jsonl.py
```

脚本同步维护位置：

- 工作区：`scripts\build_clean_corpus_jsonl.py`
- 生产目录：`D:\production\corpus\scripts\build_clean_corpus_jsonl.py`

领域话题体系构建脚本：

```powershell
powershell -ExecutionPolicy Bypass `
  -File D:\production\corpus\scripts\run_topic_taxonomy.ps1
```

相关文件：

- 主脚本：`D:\production\corpus\scripts\build_topic_taxonomy.py`
- 运行入口：`D:\production\corpus\scripts\run_topic_taxonomy.ps1`
- 种子体系与算法配置：`D:\production\corpus\configs\topic_taxonomy_config.json`
- 清洗与标题识别规则：`D:\production\corpus\docs\语料清洗与标题识别规则.md`
- 处理原则与审核方案：`D:\production\corpus\docs\领域话题体系构建与人工核验方案.md`

## 当前输出

输出目录：`D:\production\corpus\processed`

2026-07-28 本次完整话题结果位于 `D:\production\corpus\processed\taxonomy_candidates_v2`。重建时旧目录中的 `topic_review_queue.csv` 被外部程序占用，标准目录无法原子覆盖；关闭占用该 CSV 的 Excel 或编辑器后，可再次运行话题脚本将 `taxonomy_candidates_v2` 的完整结果发布到标准目录。

| 文件/目录 | 内容 |
|---|---|
| `jsonl\documents.jsonl` | 文档级语料，每条为一篇清洗后的文档 |
| `jsonl\sections.jsonl` | 章节/标题级语料，保留一级标题、二级标题等层级 |
| `jsonl\paragraphs.jsonl` | 段落级语料，每段关联所属章节路径 |
| `jsonl\sentences.jsonl` | 句子级语料，每句关联文档、章节、段落 |
| `metadata\documents.csv` | 文档元数据与统计，包括章节数、段落数、句子数 |
| `clean_text_by_source\` | 按来源类型和来源分文档保存的清洗 TXT |
| `quality\cleaning_report.json` | 清洗、跳过、去重和产物统计报告 |
| `taxonomy_candidates\candidate_taxonomy.json` | 种子分类树、机器学习候选聚类和文本证据 |
| `taxonomy_candidates\section_topic_candidates.jsonl` | 章节级候选主分类、知识维度和聚类结果 |
| `taxonomy_candidates\topic_review_queue.csv` | 候选话题人工核验队列 |
| `taxonomy_candidates\cluster_review_queue.csv` | 仅包含机器学习发现簇的专项核验队列 |
| `taxonomy_candidates\term_review_queue.csv` | 候选术语人工核验队列 |
| `taxonomy_candidates\heading_cleaning_audit.csv` | 章节标题清理前后对照和质量标记 |
| `taxonomy_candidates\run_report.json` | 话题体系生成参数、数量和运行状态 |

## 当前统计

最近一次生成结果：

- 输入可用清单：130 条
- 最终文档：75 篇
- 章节/标题节点：1,790 个
- 段落：39,331 段
- 句子：65,769 句
- 删除练习题段落：635 段
- 删除重复页眉：311 处
- 清洗后总字符数：3,894,723
- 规范化字符数：3,274,143

最近一次领域话题体系候选生成结果：

- 输入文档：75 篇
- 审计章节节点：1,790 个
- 排除文档根节点：75 个
- 排除无直接正文的章节：259 个
- 排除空标题或噪声标题：1 个
- 实际处理章节：1,455 个
- 种子话题节点：28 个
- 机器学习候选聚类：24 个
- 综合话题审核行：52 行（28 个受控种子节点＋24 个发现簇）
- 获得种子话题候选的章节：1,235 个
- 获得知识维度候选的章节：1,342 个
- 结果状态：待人工核验

## 数据结构

`documents.jsonl` 主要字段：

- `doc_id`
- `title`
- `domain`
- `topics_auto`
- `source_type`
- `source`
- `quality`
- `paragraph_count`
- `section_count`
- `sentence_count`
- `source_url`
- `source_date`
- `exercise_paragraphs_removed`
- `running_headers_removed`
- `domain_relevance`
- `clean_text_path`
- `original_path`
- `text`

`sections.jsonl` 主要字段：

- `doc_id`
- `section_id`
- `parent_section_id`
- `level`
- `heading`
- `heading_path`
- `start_paragraph_index`
- `end_paragraph_index`
- `paragraph_count`
- `heading_rule`
- `heading_confidence`
- `text`

`paragraphs.jsonl` 主要字段：

- `doc_id`
- `paragraph_id`
- `paragraph_index`
- `section_id`
- `section_level`
- `section_path`
- `is_heading`
- `text`

`sentences.jsonl` 主要字段：

- `doc_id`
- `sentence_id`
- `sentence_index`
- `sentence_index_in_paragraph`
- `paragraph_id`
- `paragraph_index`
- `section_id`
- `section_path`
- `is_heading`
- `text`

## 处理方法

1. 读取 `D:\production\corpus\analysis\corpus_inventory_details.csv` 中去重后保留且质量为“完整可用”或“可用短文本”的条目。
2. 对爬虫 TXT 读取 `URL/TITLE/DATE` 元数据，按正文标记、重复标题和站点尾部标记确定正文边界。
3. 清除网页导航、登录/打印/字号控件、版权与站点信息、相关文章、采购资讯等模板噪声。
4. 删除练习题及答案块，并删除教材中重复出现的短页眉。
5. 对爬虫文章执行主页/列表页识别和领域相关性校验，避免只偶然提及领域词的综合网页进入语料库。
6. 修复 OCR 或 PDF 抽取带来的硬换行，尽量还原自然段。
7. 根据标题模式识别章节层级；日期、时间、表格数字、目录点线、长条款和正文式编号句不得作为标题。
8. 基于章节栈生成 `section_id`、`parent_section_id` 和 `heading_path`，记录 `heading_rule` 与 `heading_confidence`。
9. 将自然段和句子写入相应 JSONL，并保留所属章节路径。
10. 对清洗后的全文再次哈希去重，跳过清洗后重复文本。
11. 每次完整重建时先重建程序管理的 `clean_text_by_source` 子目录，避免新规则淘汰的旧 TXT 残留；随后按 `source_type/source/doc_id__title.txt` 保存文本，并生成元数据和清洗报告。

## 领域话题体系构建方法

领域体系采用两个相互独立的标注轴：

- 主分类树按技术对象或产业对象组织，回答“内容主要讲什么”，例如光伏发电、动力电池、驱动电机。
- 知识维度回答“从什么角度讲”，例如政策法规、产业市场、安全与可靠性、标准与检测。

章节是主要分析和标注单元，文档标签由章节结果聚合产生。自动处理流程为：

1. 清理章节标题中的目录点线、页码、编号和图表残片，并保存清理审计表。
2. 使用可版本化种子体系匹配候选话题和知识维度，保存分数与证据词。
3. 提取领域术语、中文字符特征、英文缩写和标题短语。
4. 计算 TF-IDF，并使用球面 K-means 对章节进行话题聚类。
5. 为每个聚类汇总关键词、高频标题、代表章节和种子话题分布。
6. 生成候选体系、章节候选标签、术语审核表和两种话题审核表。
7. 可选调用兼容 OpenAI Chat Completions 的大模型，提出名称、父节点、定义、拆分和合并建议。
8. 所有自动结果保持“待人工核验”状态，不直接修改正式体系。

启用大模型前，应通过环境变量设置经过批准的接口、模型和密钥：

```powershell
$env:TOPIC_LLM_API_KEY = '在本机安全设置，不写入代码或配置'
$env:TOPIC_LLM_MODEL = '机构批准使用的模型名'
$env:TOPIC_LLM_BASE_URL = 'https://api.openai.com/v1'

powershell -ExecutionPolicy Bypass `
  -File D:\production\corpus\scripts\run_topic_taxonomy.ps1 `
  -UseLlm
```

如果语料不允许发送到外部服务，不得启用外部大模型；可以保持默认的本地规则与机器学习流程，或接入机构内部兼容接口。

## 下一步工作

`candidate_taxonomy.json` 中存在两类性质不同的数据：

- `controlled_seed_nodes` 是人工配置的受控种子体系，结构稳定、质量较高。
- `candidate_clusters` 是从当前语料通过 TF-IDF 与聚类发现的候选簇，必然更嘈杂，只能作为扩展、拆分或合并线索。

旧版 `topic_review_queue.csv` 只展开了 `candidate_clusters`，没有包含受控种子节点，也省略了部分证据，因此看起来与 JSON 中的种子体系质量差距很大。当前版本已改为：

- `topic_review_queue.csv`：统一列出 28 个受控种子节点和 24 个发现簇，通过 `row_type` 区分。
- `cluster_review_queue.csv`：只列出 24 个发现簇，供集中审核新话题。
- 两张表均保留代表标题、正文摘录、种子话题分布、质量标记和建议动作。

下一步应优先完成候选体系的人工核验和正式版本发布：

- 先审核 `topic_review_queue.csv` 中的受控节点边界，再审核 `cluster_review_queue.csv` 中的24个机器发现簇。
- 对候选话题作通过、改名、改父节点、拆分、合并或拒绝处理。
- 审核 `taxonomy_candidates\term_review_queue.csv` 中的规范术语、别名和归属节点。
- 为正式类别补充定义、纳入范围、排除范围和代表性章节。
- 发布经过审核的 V1.0 正式分类体系，并保留变更日志。
- 使用正式体系重新生成章节级和文档级标签。
- 术语上下文例句库
- 章节级与段落级检索索引
