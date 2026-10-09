# db

PostgreSQL 数据库结构及迁移文件。

- `migrations/`：按编号顺序执行的 SQL Migration；
- `0001_corpus_registry.sql`：Corpus Registry 第一版 Schema。
- `0002_recognition_validation.sql`：识别运行、候选、独立验证、交叉验证、综合决策和人工 provenance 的增量 Schema；不保存语料正文。

Migration（数据库迁移）：对数据库结构进行可追踪、可重复执行的版本化变更。新结构应新增更高编号的 SQL 文件，不能回头修改已经在真实数据库执行的迁移。

执行入口：`python -m corpus_registry init-db`。数据库密码不得写入本目录。
