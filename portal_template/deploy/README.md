# deploy

未来服务器部署和环境标准化配置。当前阶段 5–9 的开发以 Windows 本机、Conda 和原生 PostgreSQL 为主，不要求 Docker。

- `compose.db.dev.yml`：可选的 PostgreSQL 16 Docker Compose 开发服务；
- 未来 FastAPI、Frontend 和 Caddy/Nginx 配置也应放在本目录，并按环境分文件；
- 密码通过环境变量或本地 `deploy/.env` 提供，不提交版本库；
- 数据卷、备份和运行状态不进入 Git。

不要删除 Docker 配置；正式服务器路线确定前，它只是可选方案。

多用户门户的文件清单、安装与 HTTPS 配置见 [portal/README.md](portal/README.md)。
