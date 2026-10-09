# corpus_registry

PostgreSQL Corpus Registry 的 Python 接口。它管理语料库、版本、文件、任务和查询日志，但不保存结构化语料正文或 BCC 索引内容。

## 公共能力

- `initialize_database()`：执行尚未应用的 Migration；
- `CorpusRegistry`：语料、版本、文件、任务和日志 CRUD；
- `list_corpora()`、`list_versions(corpus_id)`：阶段 6 API 使用的只读列表接口；
- `inspect_release_files()`：只读检查 Release 文件；
- `python -m corpus_registry ...`：初始化、注册、查看和一致性检查入口。

## 配置

数据库从 `DB_HOST`、`DB_PORT`、`DB_NAME`、`DB_USER`、`DB_PASSWORD` 读取，语料根目录从 `CORPUS_DATA_ROOT` 读取。本机密码只放在被 Git 忽略的 `.env`。

## 维护边界

- Migration 只追加，不修改已经执行的历史 SQL；
- 项目连接使用 `corpus_user`，不使用 `postgres` 超级用户；
- Release 路径保存为相对数据根目录的路径；
- 修改后运行 `tests/test_corpus_registry.py` 的静态与真实 PostgreSQL 测试。

当前接口以本模块源码与 tests/ 中的回归检查为准；操作说明见主 README。
