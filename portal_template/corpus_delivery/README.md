# 交付与只读检索工具

复用原有 Corpus Release 校验、CorpusService 和 LangSC.BCC，不修改已有正文、索引或检索算法。

- `python -m corpus_delivery.export <Release目录> <目标releases目录>`：复制一个完整已发布版本，源与目标均校验，拒绝覆盖。
- `python -m corpus_delivery.catalog <releases目录>`：校验并读取文件目录，不连接 PostgreSQL。
- `python -m corpus_delivery.search --releases <releases目录> --host 127.0.0.1 --port 8001`：运行独立只读检索。
- `python -m corpus_delivery.builder`：加载使用者自己的 .env，通过统一平台入口启动本地构建网页。

只读服务仅注册查询所需接口，未注册构建、上传、任意文件下载或整库导出接口。公开入口由部署者配置；本地默认只监听回环地址。

本次三个交付包及详细使用说明见 `../deliveries/2026-10-09/README.md`。该目录已同步重建为当前干净构建包、空成果导出容器和最小只读服务，不含历史语料、账号或实际 .env，仍从 Git 跟踪范围排除。GitHub 已同步当前源码和新版发行包。

验证：`python -m unittest tests.test_corpus_delivery`。

维护重建工具：scripts/maintenance/build_share_packages.py，使用新的输出目录，拒绝覆盖。详见 docs/项目清理与共享维护.md。
