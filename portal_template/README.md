# “中文+”语料库平台

本项目把账号登录、独立项目、语料上传、清洗、话题体系建设、术语识别、结构化、索引、版本发布、查询和只读分享整合在一个平台中。

本文件是当前版本的主使用说明。默认操作入口为“登录 → 我的项目 → 六步构建 → 发布与分享”；用户数据按项目编号独立保存。

当前开发机的唯一项目目录是：

```text
D:\production\corpus
```

项目代码、配置、测试、文档和语料资产都以这里为准。Conda 和 PostgreSQL 是系统软件，可以安装在项目目录之外。

## 最快开始

1. 双击 `启动语料库平台.cmd`；首次使用在启动窗口创建登录账号。
2. 浏览器打开 `http://127.0.0.1:8000/`，登录后创建或选择自己的项目。
3. 在项目的原构建工作台上传并勾选本次文件，按六个步骤加工和发布。
4. 第 6 步的“分享检索 / 管理链接”可生成和撤销只读检索链接。

如果平台已经运行，双击入口只会重新打开网页，不会重复占用 8000 端口。启动窗口会显示结果并等待按键关闭；如果启动失败，请保留窗口中的错误文字。平台第一次启动时，该窗口就是服务控制台，使用期间不要关闭。

管理员命令行检查（默认检查原主项目数据，并非某个登录用户的项目）：

```powershell
conda activate corpus-platform
Set-Location D:\production\corpus
python -m corpus_platform doctor
python -m corpus_platform status
```

## 多用户独立构建与分享

### 登录与账号管理

首次双击主启动器，在启动窗口创建账号，密码最少 **3 个字符**、最多 128 个字符；输入密码时不会显示。已有账号后直接网页登录。忘记密码或需要更多账号时，由管理员在项目根目录的 Python 环境执行：

```powershell
python -m corpus_platform portal add-user user02
python -m corpus_platform portal reset-password user02
```

上面两条分别用于新建账号和重置已有账号，按实际需要执行；`user02` 是示例，请换成目标账号。重置密码会撤销该账号已有登录会话，需要重新登录。网页没有开放自助注册。

### 完成构建并分享

1. 登录后进入“我的语料库项目”，新建项目或打开已有项目。
2. 在原六步构建工作台上传、选择语料并完成清洗、话题、术语、结构化、索引和发布。
3. 第 6 步点击“分享检索 / 管理链接”，选择已发布且已建立索引的版本、设置期限，再生成链接。项目列表中的“分享检索”也可完成同样操作。
4. 当场复制并保存完整链接，发送给使用者；链接原文不会在刷新后重新显示，遗失时可以重新生成。
5. 接收者无需账号，只能检索所选版本；需要停止访问时，在同一面板撤销链接。

链接固定对应一个版本；后续发布的新版本不会自动替换旧链接。分享不会复制整套语料库到另一个用户目录，也不会开放构建、修改或整库下载。服务器、数据库和对应索引文件持续可用，且链接未过期或撤销时，检索可持续使用。

本机 `127.0.0.1` 地址仅供本机体验。其他电脑要访问，需要部署或配置网络可达的服务地址，并把 `.env` 的 `PORTAL_PUBLIC_URL` 设为实际访问地址，详见 [服务器部署说明](deploy/portal/README.md)。

原双击入口和 `python -m corpus_platform serve` 已统一进入登录、独立项目和构建主流程，默认地址为 `http://127.0.0.1:8000/`。首次双击会在终端创建账号，之后可以创建更多账号。各项目独立存储和运行，分享按钮直接位于第 6 步发布区域。

原单用户工作台仅供管理员兼容访问：设置一个空闲的 `API_PORT` 后运行 `python -m corpus_platform serve --legacy`，或 `run.ps1 -Stage legacy-api`。它仍使用原数据目录，不自动把历史成果分配给新账号。

见 [操作与功能说明](corpus_portal/README.md) 和 [服务器文件清单及部署说明](deploy/portal/README.md)。本机共享目录和 ZIP 已同步为当前版本；GitHub 已同步当前源码和新版发行包。

## 多用户构建验收与程序更新

本地验收修复了推荐话题运行环境被拒绝、轮次样本被再次抽样、Windows 编译缓存长路径及共享检索原生引擎跨项目串读问题。已有项目程序副本已同步并备份；请在运行任务结束后重启主服务使修改生效。验证范围、复现方法与真实句法服务的边界见 [多用户构建与共享验收](docs/多用户构建与共享验收.md)。

## 当前维护与共享状态

旧历史数据、验收资产、一次性开发脚本及过时阶段文档已清理。现有账号与用户项目保留。共享目录及 ZIP 已重建；第二部分为空成果导出容器，不再附带旧语料。[清理范围与维护方法](docs/项目清理与共享维护.md)。

## 当前固定流程

```text
登录账号
→ 创建或选择自己的项目
→ 上传并选择语料
→ 语料清洗
→ 抽取全量有效语句的 1/3 建立并审核三级话题体系
→ 用正式话题体系标注全量句子或完整对话话轮
→ 全量术语基础识别
→ 可选外部模型逐句识别并与基础结果求交集
→ 可选话题—术语交叉检验
→ 写入句子序号、来源、三级话题和术语标记
→ 创建 Workspace、BCC 索引和不可覆盖的 Release
→ 登记 PostgreSQL Corpus Registry
→ 项目内网页/API 查询与导出
→ 创建指定版本的只读分享链接
→ 他人无需登录远程检索
```

话题体系的机器标注和人工冲突裁决在独立弹窗中完成。交叉检验是可选步骤。未配置外部模型时，术语流程仍可使用本地基础识别正常运行。

调试后续链路时，话题弹窗允许在 BERTopic 聚类完成后使用“测试捷径”直接发布非离群簇。该体系会明确标记为测试结果，不改写正式 canonical taxonomy，不能作为正式话题成果。

## 不同用户的文件保存在哪里

新功能按**用户拥有的项目**保存文件。文件夹使用项目 UUID（唯一编号），不是用户名；同一个账号可以创建多个项目，不同账号即使上传同名文件也存入各自的项目目录。

当前默认项目根目录为：

```text
D:\production\corpus\data\portal\projects\<项目UUID去掉连字符>\
```

打开项目工作台，网址形如 `/p/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx/`。取出这段项目 UUID，去掉 `-`，即可找到对应文件夹。该账号拥有哪些项目由 PostgreSQL 记录；不能仅凭文件夹名称判断用户名。

下表中的 `data/`、`inputs/`、`configs/` 等路径，**多用户模式下均相对于该项目根目录**。例如用户上传的 TXT 位于 `<项目根目录>/data/raw/manual/`，正式语料库位于 `<项目根目录>/data/releases/chinese-plus/<版本号>/`。

本次集中清理已删除原单用户项目的历史语料、轮次和旧正式版本；对应主目录保留为空接口。现有用户项目独立保留。需导入外部正式版本时，按 [多用户详细说明](corpus_portal/README.md#导入旧正式版本) 操作。

管理员若在 `.env` 设置 `PORTAL_STORAGE`，项目目录相应变为 `<PORTAL_STORAGE>/projects/<项目UUID去掉连字符>/`。迁移和备份需保留全部项目目录及 PostgreSQL；只备份正文文件不能保留完整的任务、项目归属和分享授权。

## 输入和输出去哪里找

| 内容 | 位置 | 说明 |
| --- | --- | --- |
| 上传的原始语料 | `data/raw/manual/` | 永久保留，程序不改写 |
| 网页采集语料 | `data/raw/web/` | 原始采集结果 |
| 上传的参考文件原件 | `data/raw/reference_uploads/` | 按类别留档并记录 checksum |
| 当前生效词表和分词资源 | `inputs/linguistic/` | 页面上传后规范化、合并并去重 |
| 当前参考话题体系 | `configs/topics/new_energy_candidate_taxonomy.json` | 话题体系工作的参考输入，不等于最终体系 |
| 话题构建 JSONL 上传 | `data/workspaces/chinese-plus/topic-workflow/workflow/uploads/` | 话题页面上传的输入副本 |
| 话题构建各轮结果 | `data/workspaces/chinese-plus/topic-workflow/workflow/rounds/R0001/` 等 | 各轮样本、模型结果、审核及任务记录 |
| 当前自动加工结果 | `data/processed/` | 可重新生成，不要手工修改 |
| 当前正式话题体系 | `data/curated/topics/taxonomy.json` | 话题工作台发布后的正式结果 |
| 当前正式术语表 | `data/curated/terms/terminology.csv` | 术语识别完成后的正式结果 |
| 每次识别的中间证据 | `data/workspaces/chinese-plus/recognition_*/recognition/` | 候选、验证、交叉检验和决策 |
| 可修改任务工作区 | `data/workspaces/<corpus_id>/<workspace_id>/` | 可重复加工 |
| 正式发布版本 | `data/releases/<corpus_id>/<version>/` | 长期资产，禁止手改或覆盖 |
| 构建进程日志 | `logs/worker.log` | 各项目独立的构建服务日志 |
| 网页操作记录 | `logs/runtime/platform-operations.log` | 当前项目的网页运行记录 |
| PostgreSQL 表结构 | `db/migrations/` | 只保存管理数据结构，不保存语料正文 |
| 页面和 API | `corpus_api/` | FastAPI、操作台、查询和导出 |
| 测试 | `tests/` | 修改代码后运行回归测试 |

更完整的“功能—输入—输出—代码”对应关系见 [功能与文件位置](docs/功能与文件位置.md)。

## 只需要看这些说明

- [文档导航](docs/README.md)：按身份选择要看的文档。
- [统一加工流程与操作台](docs/统一加工流程与操作台.md)：普通用户如何完成一次构建。
- [功能与文件位置](docs/功能与文件位置.md)：每项功能、输入和输出在哪里。
- [全流程运行与迁移指南](docs/全流程运行与迁移指南.md)：启动、诊断、迁移和服务器准备。
- [目录结构与维护规范](docs/目录结构与维护规范.md)：哪些目录能改、能删或必须保护。
- [当前开发状态](DEVELOPMENT_STATUS.md)：已完成能力和现有限制。

## 三条保护规则

1. 不删除或覆盖 `data/raw/` 和 `data/releases/` 中的真实语料资产。
2. BCC 建索引使用 GB18030 兼容的 `corpus_index_syntax_tree.txt`，不是普通文本索引。
3. PostgreSQL 只保存 corpus、版本、路径、checksum、任务和日志等管理信息，不保存结构化正文或 BCC 索引内容。

机器开发助手必须先读取根目录 [AGENTS.md](AGENTS.md)。第三方许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 可分发交付成果

本次构建发行包、可迁移语料数据包和独立只读检索服务位于 [deliveries/2026-10-09](deliveries/2026-10-09/README.md)，分别附运行 README 和校验信息。数据包不上传 GitHub。只读检索工具说明见 [corpus_delivery](corpus_delivery/README.md)。
