# corpus_pipeline

现有语料加工流程的稳定 Python 接口。网站、本地工作台、命令行和自动任务应调用本包，而不是直接依赖 `scripts/` 内部文件位置。

## 公共入口

- `CorpusPipeline`：加载配置并调度既有阶段；
- `clean()`：盘点并清洗语料；
- `structure()`：质量检查并生成既有结构化输出；
- `run(stage)`：运行指定阶段；
- `python -m corpus_pipeline <stage>`：命令行入口。

`scripts/pipeline.py` 和根目录 `run.ps1` 是兼容入口，最终都会转交给本包。

## 维护边界

- 不改变 `Doc/#Global/#Group/Item:` 和句法树结构；
- 路径从 `configs/pipeline.json` 或调用参数读取；
- 新调用方只依赖 `corpus_pipeline.__init__` 暴露的接口；
- 修改后至少运行 `tests/test_corpus_pipeline.py`、`tests/test_corpus_cleaning.py` 和 `tests/test_workflow.py`。

当前操作见根目录 README；加工接口和阶段定义以本模块及 scripts/workflow/ 为准。
