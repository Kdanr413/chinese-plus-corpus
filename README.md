# 语料库构建发行包（Windows x64）

## 这是什么

本包复用原平台的网页构建流程：上传文本 → 清洗 → 话题发现和审核 → 全量话题标注 → 术语识别 → 结构化 → BCC 索引 → 不可覆盖的 Corpus Release → 检索。
带有可解包的 Python 3.11 核心环境和 LangSC.BCC，不需要安装开发工具或使用原作者的电脑路径。**首次完整构建仍需安装话题依赖、下载模型并配置自己的 PostgreSQL。** 本包不是已经完成这些准备的离线全环境镜像。
不包含原作者的语料、数据库、密码、API Key、个人路径、历史审核或运行日志。随包保留的新能源参考体系是可选示例，不是使用者已经审核的正式体系。

## 运行条件与首次准备

1. Windows 10/11 x64，建议至少 16 GB 内存；留出运行环境、话题依赖、模型和语料所需磁盘空间（建议 10 GB 起，随语料量增加）。先将整个 ZIP 解压到固定目录，如 `D:\CorpusTool`。不要直接从压缩包中运行。
2. 双击 `install-topics.cmd`。它会解包核心运行环境，联网安装 BERTopic 等依赖并下载 `BAAI/bge-small-zh-v1.5`。首次可能耗时较长；失败时检查网络，再次运行即可。依赖版本范围写在 `requirements-topics.txt`，首次安装环境不是所有间接依赖均严格锁定的离线镜像。
3. 准备 PostgreSQL。可以安装原生 PostgreSQL，建立专用数据库和用户；或者先安装 Docker Desktop，采用下方容器方式。两种方法任选其一。
4. 将 `.env.example` 复制为 `.env`，填写你自己的 `DB_HOST`、`DB_PORT`、`DB_NAME`、`DB_USER`、`DB_PASSWORD`。本地模式保持 `API_HOST=127.0.0.1`、`API_PORT=8000`、`CORPUS_DATA_ROOT=data`。不要把 `.env` 发给别人。
5. 双击 `init-database.cmd`，应用平台数据库表结构。它不会替你创建 PostgreSQL 用户或数据库；只在你配置的专用数据库应用待执行的迁移。
6. 双击 `start-builder.cmd`，浏览器打开 `http://127.0.0.1:8000/`。控制台窗口需持续打开，Ctrl+C 停止服务。

Docker 数据库方式：在本包目录打开 PowerShell，配置好 `.env` 后运行：

```powershell
docker compose --env-file .env -f compose.database.yml up -d
```

如果本机 5432 端口已使用，请在 `.env` 中选择其他空闲 `DB_PORT`。数据库容器的数据保存在 Docker 卷中，卸载或迁移时需另行备份，不能把它当临时缓存。

## 构建自己的语料库

- 上传自己的 UTF-8 TXT 等页面支持的文本格式；PDF、Word 或扫描件应先在外部转换为可用文本。本发行包没有分发原作者的文献 PDF 或 OCR 软件。
- 在 `configs/pipeline.json` 中调整 `project.name`、`project.domain` 和 `project.corpus_id`。默认领域及提示词示例仍以新能源为背景；做其他领域时必须同步调整参考体系、话题提示词和词表，不能只改标题。
- `inputs/` 中的个人词表已清空，只保留文件接口和少量通用停用词。可以通过网页上传自己的词表、参考体系、分词资源。动态正式体系初始为空。
- 按网页顺序完成语料清洗、话题聚类、多模型审核与人工裁决、全量标注、术语识别和可选交叉检验，再创建索引和发布版本。
- 外部模型审核由使用者自行填写地址、模型和 Key；没有填写就不能完成需要模型审核的环节。模型调用可能有第三方费用，由使用者自己的账户承担。
- 每次发布使用新版本号，不要覆盖 `data/releases/<语料库ID>/<版本>/`。正文保持 GB18030 和原特殊标记。

## 检查、迁移与分享

首次运行后，PowerShell 中可执行：

```powershell
.\runtime\python.exe -m corpus_platform doctor
.\runtime\python.exe -m corpus_platform status
.\runtime\python.exe -m corpus_platform verify --corpus-id chinese-plus --version <已发布版本> --offline
```

`doctor` 需在正确填写 `.env`、准备数据库后执行；输入词表或原始语料未上传时，状态提示待完成属正常。

导出一个已经建好的、带原生索引的 Release：

```powershell
.\runtime\python.exe -m corpus_delivery.export data/releases/chinese-plus/0.1.0 D:/Export/releases
```

工具先后校验源与目标，目标存在就拒绝覆盖。迁移的是完整 `corpus/`、`index/`、`metadata/`、`statistics/` 和 `manifest.json`，不能只复制网页或正文。

在同一包中双击 `start-search.cmd`，可在 8001 端口运行只读检索，不需要数据库。也可以把导出的 releases 目录放到独立的检索服务包中。对外只分享检索服务地址，不公开构建页面的 8000 端口。

重新移动已解包的运行环境时，建议在新的空目录重新解压完整发行包，再复制自己的输入、数据和本地配置；核心环境的重定位只会在首次解包时执行。

## 排错

- 页面无法打开：看启动窗口报错，检查端口是否已占用，不要关闭服务窗口。
- 话题依赖或模型未找到：先运行 `install-topics.cmd`；避免手工指定原作者电脑上的 Python 路径。
- 数据库连接失败：检查 PostgreSQL 是否运行，数据库与用户是否已创建，以及 `.env` 是否正确；不要把完整密码发到公开 Issue。
- 没有可检索版本：完成发布；独立检索服务只读取存在索引且校验通过的 Release。
- 此 Windows 运行环境不能直接用于 macOS/Linux。其他系统应按 `requirements.txt` 建立环境，并验证 LangSC 对应原生库；本次只验证 Windows x64。

第三方运行环境保留各软件原许可证，项目第三方说明见 `THIRD_PARTY_NOTICES.md`。本次交付未替原项目增加新的开源授权条款。

## GitHub 源码与完整发行包

直接下载 Release 的 Windows ZIP 可获得随附核心运行环境。仅克隆源码分支不会下载运行环境：请同时下载 Release 完整包，或自行准备 Python 3.11 与 requirements.txt 所列依赖。验证范围见 `VALIDATION.md`。
