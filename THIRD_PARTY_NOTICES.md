# 第三方组件说明

- Jieba 0.42.1：中文分词组件，MIT License。项目主页：https://github.com/fxsjy/jieba
- LangSC 2.0.7：GPF、BCC 与 JSS 语言结构计算组件；本项目以 BCC 作为语料检索引擎，MIT License，作者 endongxun。许可证文本随安装包保存在 `langsc-2.0.7.dist-info/licenses/LICENSE`。

Jieba 在 `vendor/` 中封装运行所需版本；LangSC、PostgreSQL Driver 和本地 API 依赖统一由 `requirements.txt` 锁定，其中 LangSC 依赖平台本机动态库。更新第三方组件前必须执行对应回归测试并更新本文件。

FastAPI、Starlette、Pydantic、Uvicorn 和 HTTPX 用于本地 HTTP API、JSON Schema、ASGI 服务及自动化接口测试，版本由 `requirements.txt` 固定。它们不替代 Corpus Registry、Corpus Release 或 LangSC.BCC。

本项目保留当前默认分词、词性标注及 HMM 所需 Jieba 资源；未使用的可选 Paddle/LAC 模型目录已在本次清理中移除。
