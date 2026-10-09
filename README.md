# 中文+语料库：可复用构建与共享流程

本仓库提供当前完整源码；Windows 便携包见 [最新发行版](https://github.com/Kdanr413/chinese-plus-corpus/releases/latest)。仓库保持私有，接收者需要获得仓库访问授权。

## 从源码运行

推荐使用 Conda 创建 Python 3.11 环境：

```powershell
conda env create -f environment.yml
conda activate corpus-platform
Copy-Item .env.example .env
# 配置自己的 PostgreSQL 数据库及 .env 中的 DB_* 参数
python -m corpus_registry init-db
python -m corpus_platform serve --setup
```

首次运行根据提示创建管理员，然后访问 http://127.0.0.1:8000/。密码至少 3 位。话题模型需要首次联网下载，话题审核接口和真实句法服务需要自行配置。环境诊断：`python -m corpus_platform doctor`。源码启动请使用上述命令；下方双击启动方式适用于发行 ZIP，源码仓库不携带大型便携环境。

账号登录后创建独立项目，上传语料并完成六步构建。不同项目保存在 `data/portal/projects/<项目编号>/`，包含各自上传原件、加工轮次、配置与正式版本；账号、会话和授权由 PostgreSQL 管理。第 6 步生成只读分享链接。公网使用见 [部署说明](deploy/portal/README.md)，使用和保存位置见 [主说明](PLATFORM_GUIDE.md) 与 [多用户说明](corpus_portal/README.md)。

共享时优先使用正式版本的授权链接；独立检索服务器使用 `python -m corpus_delivery.search --releases data/releases --host 0.0.0.0 --port 8001`，需先导入完整的已建索引 Release。可复用打包工具见 [发行维护](scripts/maintenance/README.md)，完整流程与只读服务共用现有 BCC 引擎。

---

# 当前构建发行包（Windows x64）

本包整合账号登录、独立项目、六步构建、版本发布与只读分享，不含原作者语料、账号、密钥和历史成果。

## 首次运行

1. 安装并启动 PostgreSQL，创建自己的数据库和用户，复制 .env.example 为 .env，填写 DB_HOST、DB_PORT、DB_NAME、DB_USER、DB_PASSWORD。
2. 双击 install-topics.cmd 安装 BERTopic 等依赖并下载模型，需要联网。本包附带核心 Python 与 BCC，未附完整话题环境及模型；首次安装可能较大。
3. 双击 start-builder.cmd；首次解包运行环境后在窗口创建账号，密码至少 3 位。浏览器打开 http://127.0.0.1:8000/。使用期间不要关闭服务窗口。
4. 登录并创建项目，上传语料、按六步流程构建，第 6 步完成索引发布后生成分享链接。

主流程和文件位置见 PLATFORM_GUIDE.md 与 corpus_portal/README.md。话题审核需要使用者自行配置接口；真实句法步骤依赖 configs/pipeline.json 中的句法服务，部署者需确认可用。测试捷径或合成句法不能代替正式研究成果。

## 文件保存与维护

项目位于 data/portal/projects/<项目编号>/；其中 data/raw/manual 保存上传原件，data/workspaces 保存加工及轮次，data/releases 保存正式版本和索引。账号和授权位于自己的 PostgreSQL。备份需包含项目、数据库和本机 .env，这些都不可公开打包。

首次解包后，在本包根目录执行 runtime/python.exe -m corpus_platform portal add-user user02 新增账号，portal reset-password 重置密码。管理员检查：runtime/python.exe -m corpus_platform doctor。回归方法见 tests/README.md。

移到新目录时推荐重新解包发行 ZIP，再迁移自己的数据、数据库和配置。环境重定位在首次运行时执行。

## 分享与部署

127.0.0.1 仅本机可用；其他电脑访问须按 deploy/portal/README.md 配置网络入口和 HTTPS，并设置 PORTAL_PUBLIC_URL。接收者通过有效分享链接只读检索，服务、数据库和指定索引持续可用时可继续使用。

独立只读服务用 start-search.cmd，但需先导入完整已建索引 Release 到 data/releases。导出方法见 corpus_delivery/README.md。第三方许可证见 THIRD_PARTY_NOTICES.md。GitHub 发行版提供当前三个独立下载包。
