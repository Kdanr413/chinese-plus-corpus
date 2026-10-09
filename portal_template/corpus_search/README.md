# corpus_search

对现有 `LangSC.BCC` 的稳定 Corpus Search Engine 适配层。本包不重新实现语言学检索算法。

## 文件职责

- `engine.py`：建索引、加载索引、读取 `Org.txt`、频率/计数/KWIC 和范围分片；
- `scope_worker.py`：在独立 Python 进程中加载一个范围子索引；
- `selection.py`：生成可审计的 GB18030 派生语料；
- `errors.py`：索引、查询、依赖和选择错误；
- `__init__.py`：公共调用接口。

## 关键规则

- 建索引输入必须是 GB18030 兼容的句法结构树语料；
- 使用 `corpus_index_syntax_tree.txt`，不能使用普通 `corpus_index.txt`；
- 全库查询读取主索引；单范围查询读取 `scope_indexes.json` 指向的 BCC 子索引；
- 查询不得修改 Release；
- 不把 BCC 索引写入 PostgreSQL；
- 当前只验收单范围或全部范围，部分多范围会明确报错。

测试入口：`python -m unittest tests.test_corpus_search tests.test_langsc_native_integration -v`。

当前接口以本模块源码与 tests/ 中的回归检查为准；操作说明见主 README。
