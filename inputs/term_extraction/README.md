# 大模型术语抽取交换接口

本接口落实参考论文中的高表现方案：明确专业领域、提供两个示例、逐句直接抽取、同一任务运行五次，并保留至少两次出现的结果。提示词不要求输出思维链。

1. `few_shot_examples.jsonl`：维护至少两个经专家确认的示例；
2. 先运行独立“领域典型术语识别”，取得 `data/processed/terms/sentence_term_llm_tasks.jsonl`；
3. 设置 `TERM_LLM_API_KEY`、`TERM_LLM_MODEL`，执行 `./run.ps1 term-llm -Limit 100`；每条任务最终需执行五次；
4. 程序把完整原始回复、模型、时间、解析结果或错误写入 `data/interim/term_extraction/llm_response_records.jsonl`，并把标准化结果写入 `llm_results.jsonl`；
5. 重跑“领域典型术语识别”。程序只接受原句中真实出现、至少两次被识别且通过停用词和边界校验的术语。

如未上传结果，流程仍输出统计候选，但明确标记大模型共识结果尚未产生，不得声称已完成论文方法。
