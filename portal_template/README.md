# 中文+语料库：完整构建与共享流程

个人电脑和服务器使用同一套源码，统一从 GitHub 拉取。平台提供登录、独立用户项目、语料上传、六步构建、版本发布和只读分享。所有文件路径都相对于项目根目录；源码不包含原始语料、账号数据库、密钥、Python 运行环境或模型缓存。

## 拉取与安装

仓库保持私有，需要登录已获得仓库授权的 GitHub 账号。安装 Git、Conda 和 PostgreSQL，然后在你选择的目录执行：

```powershell
git clone https://github.com/[REDACTED]/chinese-plus-corpus.git
cd chinese-plus-corpus
conda env create -f environment.yml
conda activate corpus-platform
Copy-Item .env.example .env
```

首次完整安装请按 [安装与迁移说明](docs/发行包安装说明.md) 创建自己的数据库并填写 `.env`。已有 `.env` 时不要覆盖。准备话题模型及程序表：

```powershell
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-zh-v1.5')"
python -m corpus_registry init-db
python -m corpus_platform serve --setup
```

首次在终端创建登录账号，密码至少 3 字符；打开 http://127.0.0.1:8000/。后续启动执行 `python -m corpus_platform serve`，Windows 也可在环境安装后双击 `启动语料库平台.cmd`。使用期间保持服务运行。

目前完整构建与 BCC 原生检索验收使用 Windows x64、Python 3.11。服务器优先使用同架构 Windows；Linux/Docker 文件保留供后续验证，尚不能承诺完整原生索引链路直接运行。安装模型依赖后，仍需配置实际可用的话题审核接口和真实句法服务；不得把测试合成语法当作正式成果。

## 个人电脑与服务器

个人电脑：默认仅本机访问。要分享给其他电脑，配置对方能访问的主机地址或域名，并使服务保持在线。

服务器：仍按上述方式 git clone 和安装，在 [服务器部署说明](deploy/portal/README.md) 中完成公网入口、HTTPS、数据库和常驻运行设置。用户登录后独立创建项目，在网页上传原文并完成构建。不要把项目文件夹配置为公开静态网站。

## 上传与构建

登录 → 创建项目 → 上传 TXT 或只含 TXT 的 ZIP → 按六步构建 → 发布和建立索引。需要人工审核时，按工作台提示完成审核后继续。

人工参考话题体系使用 UTF-8 层级 JSON：

- [最小上传模板](参考文件中心/模板/参考话题体系.json)
- [完整填写示例](参考文件中心/模板/参考话题体系_填写示例.json)
- [字段说明与上传位置](参考文件中心/话题体系格式说明.md)

其他上传模板见 [参考文件中心](参考文件中心/README.md)。网页资源区可取得对应模板。只在自己的项目中上传，不通过修改全局配置代替上传。

## 两种分享方式

**在线链接**：第 6 步选择已建立索引的正式版本，生成只读分享链接，发给他人直接检索。可设置期限或撤销；接收者不需要 GitHub、安装代码或构建账号。主机、门户、数据库和版本资产需持续可用。个人电脑与服务器都支持这一方式，个人电脑也必须提供可访问网络入口；localhost 链接无法供远程访问。

**完整成果导出**：导出一个完整已发布的 Release，让另一台主机从同一 GitHub 拉取源码后运行独立只读检索服务。详见 [成果导出与独立检索](corpus_delivery/README.md)。独立检索不需要账号数据库；它没有门户分享链接的授权、过期和撤销功能。

## 保存位置与备份

| 内容 | 相对路径 |
| --- | --- |
| 用户项目 | `data/portal/projects/<项目编号>/` |
| 项目上传原件 | `data/portal/projects/<项目编号>/data/raw/manual/` |
| 项目参考文件原件 | `data/portal/projects/<项目编号>/data/raw/reference_uploads/` |
| 项目过程与轮次 | `data/portal/projects/<项目编号>/data/workspaces/` |
| 项目正式版本及索引 | `data/portal/projects/<项目编号>/data/releases/` |
| 新项目的干净程序模板 | `portal_template/` |

账号、会话、项目归属与分享授权在 PostgreSQL；主机配置在 `.env`。不同用户项目独立存储。备份需同时包含项目、数据库和配置；源码 ZIP 不代替数据备份。原始语料与正式版本必须保护，日常更新不得删除 `data/raw/` 或任何用户项目中的原件。

## 更新和维护

停止构建任务后备份数据与配置，再在源码目录执行 `git pull --ff-only`，按变更说明更新依赖。不要用清理 Git 工作区的命令删除本机配置或数据。新项目使用新模板；已创建项目自带代码副本，更新主仓库不会自动升级这些项目，需管理员备份后安排单独升级。

管理员新增或重置账号：`python -m corpus_platform portal add-user user02`、`python -m corpus_platform portal reset-password user02`。检查：`python -m corpus_platform doctor`；回归方法见 [测试说明](tests/README.md)。

最终交付只有一份完整源码 ZIP及校验记录，与 GitHub 默认分支源码一致，不再分发三套程序或附带核心便携环境。发行维护见 [维护说明](scripts/maintenance/README.md)。第三方许可证见 [第三方声明](THIRD_PARTY_NOTICES.md)，完整多用户说明见 [多用户功能](corpus_portal/README.md)。
