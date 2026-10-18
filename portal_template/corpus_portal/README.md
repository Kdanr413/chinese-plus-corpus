# 多用户独立构建与只读分享

每个账号登录后可以创建私有项目，在原有网页工作台上传、加工、审核、索引并发布自己的语料库。发布后，在第 6 步“分享检索 / 管理链接”或“我的项目 → 分享检索”选择一个正式版本，生成可发送给他人的链接。访问者无需账号，只能检索该版本。链接可以设置期限，也可以随时撤销。

## 统一主流程入口

双击根目录 `启动语料库平台.cmd`，首次使用在启动窗口输入账号和密码；浏览器进入登录页。登录后新建或选择项目，继续原六步构建。在第 6 步发布区域点击“分享检索 / 管理链接”，即可在当前页面选择版本、生成和撤销链接。

命令行统一入口是 `python -m corpus_platform serve --setup`；已创建账号后也可以直接 `python -m corpus_platform serve`。`portal serve` 保留为同一功能的兼容命令。

## 手动创建更多账号

在项目根目录打开已安装依赖的 Python 环境。先在根目录 `.env` 填写本机 PostgreSQL 的 DB_HOST、DB_PORT、DB_NAME、DB_USER、DB_PASSWORD。数据库账号需要创建 schema 和表的权限。

```powershell
python -m corpus_platform portal init-db
python -m corpus_platform portal add-user user01
python -m corpus_platform portal serve
```

如果平台已经运行，只需执行 `add-user` 或 `reset-password`，不用重复启动 `serve`。

创建账号时在终端输入两次密码，密码至少 3 字符，不会显示。浏览器打开 `http://127.0.0.1:8000/_portal`。第二个账号使用 `python -m corpus_platform portal add-user user02` 创建。忘记密码时由管理员执行 `python -m corpus_platform portal reset-password user02`，输入两次新密码；该账号需重新登录。当前采用管理员邀请账号，没有开放自行注册。

新项目还需要干净的程序模板。当前工作目录已准备 `portal_template/`；迁移时一起复制。如果目标主机没有模板，使用不含真实语料、历史结果和 `.env` 的构建发行目录：

```powershell
python -m corpus_platform portal prepare-template --source path/to/clean-builder
```

已存在的模板和项目不会被这个命令覆盖。旧发行 ZIP 的程序不包含本次新增的多用户模块；部署时必须复制当前代码，不能只用旧 ZIP。

## 用户操作

1. 登录，输入项目名称，创建项目。
2. 点击“打开构建工作台”，上传自己的 TXT 或 TXT ZIP；按原工作台顺序完成构建。话题建设 JSONL 使用该页面的对应入口。
3. 发布并建立索引后，直接点击第 6 步的“分享检索 / 管理链接”；也可在“我的项目”中点击“分享检索”。
4. 选择正式版本和有效期，创建链接；复制链接发给使用者。链接原文只显示一次，请当场保存；遗失时可以撤销后重新生成。
5. 接收者直接打开链接，使用检索和上下文功能。链接没有上传、加工、修改和整库下载入口。

新增版本需要另建链接，旧链接仍固定指向原版本。撤销会在下次请求生效，已经显示在他人浏览器中的结果不能收回。服务器和数据库保持运行、链接未失效、对应文件仍存在且索引可用时，分享可以持续使用。发送服务器网址即可，不需要发送 HTML 文件。

## 隔离方式

- 用户、登录会话、项目归属和分享授权存于 PostgreSQL 的 `corpus_portal` schema。
- 每个项目使用 `PORTAL_STORAGE/projects/<项目UUID无连字符>/`，包含自己的程序副本、配置、上传文件、任务、日志和结果。
- 每个项目使用独立进程及 PostgreSQL 的 `p_<项目UUID无连字符>` schema，复用原有加工与 BCC 检索能力。
- 网页路由为 `/p/<项目UUID>/...`，服务逐次核对项目归属；浏览器任务缓存也按项目隔离。
- 内部项目进程只监听回环地址，全部接口要求内部令牌，浏览器通过门户访问。
- 新项目从干净模板创建，不复制原目录中的私人语料、历史成果、`.env` 或管理员模型凭证。

这里是应用级隔离。所有项目进程仍属于同一个服务器操作系统账号；不要给构建用户操作系统权限，也不要允许上传并执行 Python 程序。当前适合由管理员创建账号的使用方式。开放注册和大规模运行需要后续补充任务队列、进程资源控制和运维措施。

## 每个用户的文件保存位置

默认所有用户项目都位于 `<主程序目录>/data/portal/projects/`。当前开发机上是：

```text
D:\production\corpus\data\portal\projects\<项目UUID无连字符>\
```

实际结构按项目编号划分，不按用户名划分。一个用户可以有多个项目；数据库的 `corpus_portal.projects.owner_id` 记录项目属于哪个用户。不要通过重命名或移动文件夹来转移项目归属。

### 从网页找到自己的目录

1. 登录，打开目标项目的构建工作台。
2. 查看网址中的 `/p/<项目UUID>/`。UUID 是形如 `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` 的编号。
3. 去掉编号中的连字符 `-`，在 `data/portal/projects/` 中找到同名目录。下表全部相对于这个项目目录。

| 内容 | 项目内相对位置 | 说明 |
| --- | --- | --- |
| 原始 TXT 与 TXT ZIP 上传结果 | `data/raw/manual/` | 通常按上传日期批次建子目录；ZIP 中的 TXT 解压到批次内，成功后上传 ZIP 临时文件删除 |
| 网页采集原文 | `data/raw/web/` | 当前项目的采集文件 |
| 参考文件上传原件 | `data/raw/reference_uploads/` | 按类别归档，包括上传词表等资源原件 |
| 生效的词表与分词资源 | `inputs/linguistic/` | 导入后的规范化资源，以该项目配置为准 |
| 清洗、目录清单与审计 | `data/interim/` | 语料清单、清洗记录等中间文件 |
| 当前清洗和结构化结果 | `data/processed/` | `corpus/`、`index/` 等子目录；专用结构化文本及标注明细 |
| 话题建设 JSONL 上传 | `data/workspaces/chinese-plus/topic-workflow/workflow/uploads/` | 话题页面接收的带时间戳输入文件 |
| 各轮话题建设结果 | `data/workspaces/chinese-plus/topic-workflow/workflow/rounds/<轮次号>/` | 如 `R0001/`，保留抽样、聚类、各模型分支、审核与任务记录 |
| 话题体系累计定稿 | `data/workspaces/chinese-plus/topic-workflow/canonical/` | 当前项目累计维护的动态话题体系 |
| 当前正式话题体系 | `data/curated/topics/taxonomy.json` | 发布后用于后续加工的正式体系 |
| 当前正式术语表 | `data/curated/terms/terminology.csv` | 术语识别和确认后的正式资源 |
| 每次识别的中间证据 | `data/workspaces/chinese-plus/recognition_*/recognition/` | 候选、检验、综合决策和审核证据 |
| 构建工作区 | `data/workspaces/<corpus_id>/<workspace_id>/` | 正式发布前使用的可修改工作区 |
| 可共享的正式语料库 | `data/releases/<corpus_id>/<版本号>/` | 完整保留 `manifest.json`、正文、BCC 索引和元数据 |
| 项目配置与处理报告 | `configs/`、`reports/` | 各项目独立；话题工作流自身配置还位于项目的 `corpus_topic_workflow/configs/` |
| 构建服务日志 | `logs/worker.log` | 该项目进程的输出和错误 |
| 网页操作记录 | `logs/runtime/platform-operations.log` | 该项目提交的网页操作记录 |

上表对应当前默认模板；若管理员改了项目中的 `configs/pipeline.json`，加工输入输出的位置以该项目配置为准。尚未使用对应功能时，某些目录和文件不会出现。

两个用户上传同名 TXT，文件分别放在各自项目的 `data/raw/manual/`，不会因同名而合并。两个项目都使用 `chinese-plus` 作为内部 corpus_id，也不会共用数据；外层项目 UUID 和数据库 schema 保持独立。同一项目当前加工结果可能更新，历史正式版本应保存到不同版本号的 Release 中。

### 管理员核对账号与目录

在 PostgreSQL 管理工具中执行下面的只读查询，即可核对账号、项目名称及文件夹编号。查询不会输出密码：

```sql
SELECT u.username AS account,
       p.name AS project_name,
       p.id AS project_id,
       replace(p.id::text, '-', '') AS directory_name
FROM corpus_portal.projects AS p
JOIN corpus_portal.users AS u ON u.id = p.owner_id
ORDER BY u.username, p.created_at;
```

账号、密码派生值、会话和分享授权存于 PostgreSQL，不是单独的用户名文件夹。分享记录在 `corpus_portal.shares`，检索仍读取拥有者项目的正式版本，不会给访问者生成语料副本。

### 自定义存储与备份

`PORTAL_STORAGE` 可在主程序 `.env` 中修改；绝对路径直接使用，相对路径相对于主程序目录解析。修改设置不会自动搬迁已经存在的项目，请先结束写入任务并迁移原存储目录，再切换设置。

完整备份至少包含整个 `PORTAL_STORAGE`、PostgreSQL 数据库、模板和主程序配置；单项目备份也应保留完整项目目录及其数据库记录。项目中可能有运行配置、模型密钥与私人原文，不应作为公开网页目录或公开 GitHub 内容。

原主程序 data/raw、data/releases 与用户项目属于不同位置。本次清理已按所有者要求删除旧主程序历史文件；现有用户项目保留，之后的备份以实际使用的数据区为准。

## 导入旧正式版本

本次清理已移除原项目旧版本与历史轮次。若另有外部备份或新正式版本需要导入，先由成果所有者登录创建项目，在工作台地址中取得项目 UUID。管理员复制正式版本到该项目：

```powershell
python -m corpus_platform portal import-release --project PROJECT_UUID --source path/to/old/releases/chinese-plus/0.0.3
```

源目录必须是完整的 Release，包含 `manifest.json`、语料和索引。导入前后均校验，保留 GB18030 和专用结构标记；同名版本存在时拒绝覆盖。原目录不会移动。导入后即可在门户分享；若项目工作台已启动，等待任务结束后重启门户，项目重新启动时登记导入版本。

历史轮次、审核记录和中间文件应另外保留在私有归档，不是可直接检索的正式版本。没有索引的成果需要完成索引与发布后才能生成分享链接。

## 设置与限制

在 `.env` 中配置，路径可以是相对于项目根目录的路径：

| 设置 | 默认值 | 作用 |
| --- | --- | --- |
| PORTAL_PUBLIC_URL | http://127.0.0.1:8000 | 浏览器实际访问的站点来源，无子路径；决定分享地址、Cookie 和请求来源校验 |
| PORTAL_STORAGE | data/portal | 所有用户项目存储位置 |
| PORTAL_TEMPLATE | portal_template | 干净模板目录 |
| PORTAL_MAX_WORKERS | 4 | 同时运行的项目工作台进程数，按内存调整 |
| PORTAL_LLM_ALLOWED_HOSTS | api.openai.com | 管理员允许的模型 API 域名，逗号分隔；只允许 HTTPS 的 443 端口 |

模型测试前会先保存当前项目的模型与步骤配置。若提示“模型接口域名……尚未获准使用”，说明请求在平台校验阶段被拦截，尚未调用模型，也不能据此判断 API Key 或模型是否有效。管理员应核对实际接口网址，在项目根目录 `.env` 的 `PORTAL_LLM_ALLOWED_HOSTS` 中添加其域名（仅域名，不含协议和路径；多个域名用英文逗号分隔），然后重启平台及项目工作服务。服务器部署时修改部署环境变量并重启服务。只添加自己实际使用并信任的服务域名。

例如使用官方 DeepSeek 接口时可配置 `PORTAL_LLM_ALLOWED_HOSTS=api.openai.com,api.deepseek.com`；第三方接口需要填写该第三方的实际域名，不能根据模型名称推断。刷新页面后重新填写或确认模型，点击保存，再测试对应步骤。`DeepSeek-V4.1-Flash` 这类带点号的名称可填写在“模型名称”栏；本地模型标识则使用字母、数字、下划线或连字符。
| TOPIC_WORKFLOW_PYTHON | 空 | 包含 BERTopic 等依赖的服务器 Python 完整路径；用户不能指定其他程序 |

每账号最多 5 个项目。门户单次请求最多 64 MiB，较大批次请拆分。上传入口按项目已用文件大小检查 10 GiB 上传额度；加工衍生文件和 ZIP 解压可能继续增长，这不是磁盘硬配额。登录会话不再按时长自动过期；主动退出会注销会话，停用账号后其会话也不能访问项目。浏览器使用无固定到期时间的会话 Cookie，关闭浏览器或清除 Cookie 后可能需要重新登录。账号或来源失败尝试达到阈值后暂时限制登录。

项目进程目前不会自动退出或淘汰。达到进程上限时，新工作台返回繁忙提示；管理员应在任务结束后重启门户或调整上限。关闭门户会结束它启动的构建进程树，因此运行任务期间不要重启。原单用户工作台通过显式 `serve --legacy` 或 `run.ps1 -Stage legacy-api` 访问；与统一平台同时运行时必须使用另一个空闲端口，且只供本机管理员使用。服务器仅公开统一平台。

模型地址要由管理员加入允许域名后才能使用，各用户填写自己的模型密钥。API 密钥可能交由原流程写入该用户项目运行配置，因此整个项目存储及备份都是私有资产。共享链接本身是访问凭证，拿到链接的人可以继续转发。

## 验证记录

已通过账号权限、CSRF、跨目录参数拒绝、版本固定、撤销、浏览器缓存分离和保留原文件的导入测试。另以真实 PostgreSQL、临时账号和两个实际工作台进程验证了登录、项目建立、上传隔离、跨账号拒绝和内部进程令牌校验。测试账号与临时项目已清理。

新增真实 BERTopic 与双账号完整链路验收工具，使用测试话题体系及本机合成句法接口。它验证真实 BCC 索引和共享权限，不证明正式多模型审核或真实句法质量。两用户同时构建、公网 HTTPS、重启恢复及备份恢复仍需验证。详见 [多用户构建与共享验收](../docs/多用户构建与共享验收.md)。

```powershell
python -m unittest tests.test_corpus_portal
# 显式启用真实数据库测试：会创建再清理临时账号、项目和项目 schema。
$env:CORPUS_PORTAL_LIVE_TEST='1'
python -m unittest tests.test_corpus_portal_live
Remove-Item Env:CORPUS_PORTAL_LIVE_TEST
```

服务器文件清单、数据库迁移和 HTTPS 配置见 [部署说明](../deploy/portal/README.md)。

同账号项目名称按规范化结果查重（空白、大小写、全半角）；重复时提示重新命名，不创建额外目录，不自动更改历史名称。步骤模型配置保存在各项目 configs/llm/step_models.json；该文件含私有模型凭据，不随源码发行。详细使用见 [步骤模型配置与项目命名](../docs/步骤模型配置与项目命名.md)。

### 项目存储额度

在项目根目录 `.env` 设置 `PORTAL_PROJECT_QUOTA_GB=100`（单位 GiB）后重启平台。新安装配置示例为100 GiB；未设置时兼容原来的10 GiB默认值。额度统计每个项目全部文件，包括原始语料、模型结果、索引和历史版本，不能代替磁盘空间检查。只限制实际上传的 POST 请求，查看已上传资源不受此额度拦截。额度不足会显示已统计占用、上传量、上限及配置字段；无需删除科研数据。目录统计在后台线程执行，避免阻塞门户响应。

### 在网页设置监听与分享地址

使用已授权账号登录“我的语料库”，点击右上角“访问与分享设置”；分享检索窗口也提供同名按钮。可填写 API_HOST、API_PORT、PORTAL_PUBLIC_URL，页面分别显示当前生效值与已保存值。保存只更新项目根目录 `.env` 的这三个字段，保留数据库密码、模型密钥和其他配置，并在 `logs/network-settings-backups/` 保存原配置备份；该目录含私有配置，不能分享或提交Git。

保存不会立即改变监听、分享链接或停止任务。等加工结束后，从原启动窗口关闭平台再正常启动；重新复制分享链接。监听0.0.0.0允许网络连接，但对外URL必须使用对方实际可访问的IP/域名，不能写0.0.0.0，不能包含 `/share/...` 路径。防火墙、校园网隔离和服务器反向代理仍需由管理员按部署环境设置。系统/启动器环境变量若有同名配置，其优先级高于.env。

首次授权由主机维护者在项目根目录运行 `python -m corpus_platform portal allow-network-admin 账号名`，重启后该账号可见设置入口。撤销时追加 `--remove` 并重启。名单存于 `PORTAL_NETWORK_ADMIN_USERS`，默认没有账号具备此权限；普通账号、未登录者与分享访问者均不可读写此设置。网页不能修改管理员名单。此授权仅管理访问与分享配置，不改变其他用户项目的所有权。
