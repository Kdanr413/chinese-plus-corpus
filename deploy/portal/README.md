# 多用户服务器部署说明

目标：用户登录后独立构建，完成后发出只读检索链接。不能把整个项目目录设为公开静态网站；网页由门户服务输出，数据目录只供服务端读取。

## 从 GitHub 获取完整源码

个人主机和服务器统一从已授权的私有仓库拉取：

```powershell
git clone https://github.com/Kdanr413/chinese-plus-corpus.git
cd chinese-plus-corpus
```

完整源码已包含程序、配置接口、干净 portal_template、上传 JSON 模板、测试及部署文件。无需上传开发机目录，不依赖 base_file 或旧三部分便携包。首次数据库准备见 [安装说明](../../docs/发行包安装说明.md)。不要复制或提交他人的 .env、data、模型缓存或账号备份。

如迁移自己的已有成果，停下写入任务后单独迁移用户项目目录，并用 pg_dump -Fc / pg_restore 恢复 PostgreSQL 到空目标库。源码不包含数据，单独复制原文不能恢复项目归属和分享授权。原始语料不得在迁移和更新中清除。

## 安装环境

当前完整隔离验证使用 Windows、Python 3.11 和原生 PostgreSQL。若要尽快复用现有 BCC 环境，可以先使用同架构 Windows 服务器。Linux 需要另行验证 LangSC/BCC 本地库与完整构建；当前不提供已经验证的 Linux Docker 全流程镜像。

在服务器安装 Conda 和 PostgreSQL，建立数据库与专用数据库账号。完成上述克隆后，在源码根目录执行：

```powershell
conda env create -f environment.yml
conda activate corpus-platform
Copy-Item .env.example .env
```

编辑 `.env` 的数据库配置；如已存在 `.env`，直接修改，不执行覆盖复制。完整依赖包括 BERTopic；之前的核心便携运行时只覆盖部分能力，不应据此声称完整主题模型环境就绪。大型模型文件还需在服务器准备并验证。

追加多用户设置，域名使用你自己的实际域名：

```dotenv
PORTAL_PUBLIC_URL=https://corpus.example.org
PORTAL_STORAGE=data/portal
PORTAL_TEMPLATE=portal_template
PORTAL_MAX_WORKERS=4
PORTAL_LLM_ALLOWED_HOSTS=api.openai.com
```

没有域名时只进行本机验证，PUBLIC_URL 设置为 `http://127.0.0.1:8000`。如果通过其他地址访问，PUBLIC_URL 必须改为浏览器实际使用的来源；不要混用 localhost、IP 和不同端口。启用 HTTPS 的 PUBLIC_URL 后，登录 Cookie 只通过 HTTPS 发送。

```powershell
python -m corpus_platform portal init-db
python -m corpus_platform portal add-user user01
python -m corpus_platform portal add-user user02
python -m corpus_platform serve
```

数据库账号需要为门户及每个项目建立 schema 和表。门户初始化只新增 `corpus_portal` 表，不迁移或覆盖原 `public` 研究数据。模板已上传时不用再创建；没有模板时按模块 README 从干净构建包创建。

## HTTPS 与外部访问

将域名解析到服务器。仅对外开放反向代理的 80、443 端口，PostgreSQL 5432、门户 8000、另设的管理员兼容端口和内部随机项目端口都不对外开放。

安装 Caddy，复制 `Caddyfile.example` 为 `Caddyfile`，把示例域名换成实际域名：

```powershell
caddy validate --config deploy/portal/Caddyfile --adapter caddyfile
caddy run --config deploy/portal/Caddyfile --adapter caddyfile
```

示例将所有请求转给本机门户，不配置 `file_server`。域名与公网连接满足条件时，Caddy 提供自动 HTTPS；配置依据 [反向代理官方文档](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy) 和 [HTTPS 官方说明](https://caddyserver.com/docs/quick-starts/https)。当前配置没有在真实公网主机申请证书验证。

服务应由操作系统服务管理器或 Windows 任务计划程序在开机启动并保持运行；先完成手动测试再配置常驻。门户启动脚本为 `start-portal.ps1`，通过 `-Python` 传入环境 Python 完整路径。必须只启动一个门户主进程；不要配置多个 Uvicorn worker，以免项目进程管理重复。

Caddy 不启用访问日志，避免分享凭证 URL 被直接写入普通访问日志。门户也不输出访问日志。若以后增加日志或监控，应对 `/share/<凭证>/` 脱敏。门户当前不信任代理头，因此登录来源限流会把同一代理后的请求归为一个来源；生产高并发场景需后续配置可信代理与独立限流。

## 不同用户的文件位置

服务器默认使用 `<项目根目录>/data/portal/projects/<项目UUID无连字符>/`，每个用户的每个项目分别拥有自己的 `data/raw/`、`data/processed/`、`data/workspaces/`、`data/releases/`、配置和日志。用户名与项目 UUID 的对应关系在 PostgreSQL 中；文件夹不直接使用用户名。设置 `PORTAL_STORAGE` 时，以上外层存储位置相应变化。

各类文件的精确子目录、从网页定位目录的方法及管理员只读查询见 [用户文件保存位置](../../corpus_portal/README.md#每个用户的文件保存位置)。只读分享访问原项目的指定正式版本，不另外复制给访问者。备份需同时保留文件和数据库归属记录。

## 旧数据与恢复

原来的全部轮次放在私有归档中。正式 Release 可以通过 `portal import-release` 复制到指定用户已经创建的项目，之后由用户自己生成分享链接，命令见 [模块说明](../../corpus_portal/README.md)。不会默认让其他用户看到原主人成果。

同时备份整个 `PORTAL_STORAGE`、模板、PostgreSQL 数据库和服务器私有 `.env`。备份要加密并限制访问。恢复时让项目 UUID 和对应目录匹配。分享记录只保存令牌摘要，因此只恢复文件不能恢复原链接；数据库和文件一起恢复才能保留原分享地址。

移到新主机后请保持同一公网域名，否则先前已经发送的链接需要更新或设置旧域名转发。运行配置中的服务器绝对路径、Python 路径、模型缓存路径可能需要管理员调整。旧项目程序副本不会自动升级，修改模板仅影响后续新建项目。

## 上线前现场核对

1. 用两个账号各建项目、上传不同文件，互相看不到项目和文件。
2. 至少实际完成一次完整加工、索引和发布，验证服务器主题模型环境。
3. 用未登录浏览器或另一台设备打开分享，只出现所选版本；撤销后再检索应失效。
4. 确认构建接口与数据库端口无法通过公网直接访问。
5. 停止正在写入的任务后测试重启与备份恢复。

本阶段完成本机多用户与分享功能，不包含真实服务器购买、域名配置、公网部署或对公开自注册用户的完整安全加固。
