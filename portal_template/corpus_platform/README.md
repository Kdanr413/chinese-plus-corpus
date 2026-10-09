# corpus_platform

这是项目的统一控制层，只负责编排已经存在的模块，不包含新的清洗、索引或检索算法。

```text
corpus_platform
├─ corpus_pipeline   清洗、语言加工、结构化
├─ corpus_releases   Workspace 与不可变 Release
├─ corpus_search     GB18030 派生语料与 LangSC.BCC 索引
├─ corpus_registry   PostgreSQL 登记与一致性校验
└─ corpus_api        FastAPI 与检索页面
```

先运行 `python -m corpus_platform status` 查看进度和下一条建议命令，再按 `docs/全流程运行与迁移指南.md` 操作。`publish` 会写入 Workspace、创建新 Release、构建索引并可登记数据库；必须先加 `--dry-run` 核对目标。它拒绝覆盖已有 Workspace、Release 和索引。
