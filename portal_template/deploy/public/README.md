# 公开站部署：只读检索与源码下载

完整配置见 [公开站说明](../../docs/公开展示与只读检索网站.md)。本目录用于独立展示服务，原私有构建平台的部署见 `deploy/portal/`。

## 服务器准备

1. 当前优先使用 Windows x64 / Python 3.11；BCC 原生库须与系统兼容。Linux 尚未验收。
2. 获取完整源码，在专用环境执行 `python -m pip install -r requirements-public.txt`。此服务不连接 PostgreSQL；完整构建另按主 README 安装。
3. 复制选定 Release 到 `data/public_site/releases/CORPUS_KEY/VERSION/`，包含完整 manifest、正文、原生索引和元数据。不要改写 GB18030 正文或冻结元数据。
4. 复制经过发行脚本生成的源码 ZIP 及同目录 `release-manifest.json`。下载模板来自源码内 `参考文件中心/模板/`。
5. 创建私有 `data/public_site/settings.json` 并明确填写允许公开的版本、源码路径、标题及使用范围。不要上传 `.env`、账号数据库、原始语料或模型密钥。

## 启动与验收

从项目目录执行：

```powershell
python -m corpus_platform public-site --config data/public_site/settings.json --host 127.0.0.1 --port 8002
```

也可在已激活环境中执行 `./deploy/public/start-public-site.ps1`；它按脚本位置定位根目录，没有固定磁盘路径。`-Python` 可指定该服务器实际 Python，`-Config` 相对根目录解析。停止此命令只停止独立展示服务，不影响另行启动的私有构建平台。

检查 `/health`、真实词频与例句检索、来源筛选、三级话题标志词和源码下载校验。仅健康检查成功不足以证明原生索引可用。源码更新后按发行校验清单替换配置，正常重启本展示服务。

## 域名与 HTTPS

有公网服务器后，把域名 DNS 指向服务器，按网络环境放行 80/443；让 Caddy 代理到本机 8002。复制 `Caddyfile.example` 到部署者自己的配置位置，将 `corpus.example.org` 换成实际域名，使用 Caddy 官方发行程序按其说明启动。证书数据目录须持久保存，域名和网络条件满足后 Caddy 可以管理 HTTPS。

示例只将请求转到公开服务，不将项目目录配置为静态文件目录，不转发到私有平台 8000。服务器不需要配置 `PORTAL_PUBLIC_URL` 来运行这个独立公开站；该变量用于原门户生成分享链接。反向代理位于同一主机时继续监听 `127.0.0.1`，无需将 8002 暴露公网。

公网开放前由管理员确认真实语料使用范围、并发容量、访问日志保存规则和备份。代码限制最多四个并发原生检索进程；这不是针对所有 API 的防滥用机制。高流量服务须按实际负载设置入口限流和资源配额。常驻运行使用系统服务或计划任务，并使用运行账户可写的日志目录；不要依赖打开浏览器维持服务。

本阶段已完成本地服务与文件准备，未自动配置 DNS、证书、防火墙或公网部署。公网配置须在实际服务器上验收。
