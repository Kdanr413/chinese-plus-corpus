# 测试

完整回归：

```powershell
python -m unittest discover -s tests -v
```

常用分组：

| 功能 | 主要测试 |
| --- | --- |
| Pipeline 与结构化 | `test_corpus_pipeline.py`、`test_workflow.py`、`test_structure_reuse.py` |
| 话题体系弹窗 | `test_topic_workflow_integration.py` |
| 话题、术语和交叉检验 | `test_corpus_recognition.py`、`test_recognition_workflow.py`、`test_evaluated_term_recognition.py` |
| API 与页面 | `test_corpus_api.py`、`test_ui_server.py` |
| Release 与 Registry | `test_corpus_releases.py`、`test_corpus_registry.py` |
| BCC 索引与查询 | `test_corpus_search.py`、`test_langsc_native_integration.py` |

真实 PostgreSQL 和原生 LangSC 测试可能需要本机环境变量。跳过测试必须说明条件，不能通过删除测试或放宽约束解决失败。

## 多用户真实构建验收

账号、页面和参数回归用 `python -m unittest tests.test_corpus_portal tests.test_topic_workflow_integration tests.test_corpus_platform`；设置 `CORPUS_PORTAL_LIVE_TEST=1` 后可运行 `tests.test_corpus_portal_live` 检查真实 PostgreSQL 和工作台进程。

完整模型、索引及分享链路由显式工具 `scripts/verify/portal_acceptance.py` 验证，需要设置 `CORPUS_PORTAL_ACCEPTANCE=1`；采用测试话题体系和本机合成句法接口。依赖、清理、报告位置与范围见 [验收说明](../docs/多用户构建与共享验收.md)。

设置 `CORPUS_PORTAL_NATIVE_TEST=1` 后运行 `python -m unittest tests.test_portal_native_search`，可用两个独立生成的真实 BCC 索引交替检查词频与上下文隔离。

步骤模型回归：`python -m unittest tests.test_step_models`。真实前端组合检查：`node tests/check_cleaning_toggle.cjs`（开发验证需 Node.js）。

公开介绍与下载站：`python -m unittest tests.test_public_site tests.test_corpus_delivery -v`，覆盖选定版本隔离、只读路由、无数据库查询、源码校验与篡改拒绝、模板下载及空配置。真实索引需额外执行词频与例句查询，Linux 原生库兼容性不能由模拟引擎测试代替。
