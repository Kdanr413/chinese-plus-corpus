# 成果导出与独立只读检索

个人电脑与服务器从同一 GitHub 获取源码。通常直接在构建平台第 6 步生成只读分享链接；接收者只需打开链接，主机及其数据库和版本资产持续在线。

需要让另一台主机自行承载检索时，在构建主机的源码目录执行（将 PROJECT_ID 与 VERSION 换成实际值）：

```powershell
python -m corpus_delivery.export data/portal/projects/PROJECT_ID/data/releases/chinese-plus/VERSION exported-releases
```

导出完整 Release，包含 manifest.json、正文、原生索引和元数据，保持 GB18030 和特殊结构标记；拒绝覆盖。来源的语料库 ID 不是 chinese-plus 时使用实际 ID。导出目标可自行选择；不要带入账号、.env、原件或密钥。

检索主机从 GitHub 拉取并按主 README 安装依赖，将完整 exported-releases 内容复制到该主机 data/releases 后：

```powershell
python -m corpus_delivery.catalog data/releases
python -m corpus_delivery.search --releases data/releases --host 127.0.0.1 --port 8001
```

打开 http://127.0.0.1:8001/search。独立只读检索不需要 PostgreSQL；原生 BCC 仍需当前平台兼容。给其他电脑访问须由管理员配置绑定地址、网络入口和 HTTPS。例如监听所有网卡用 --host 0.0.0.0，再通过实际 IP 或域名访问。

此服务开放只读查询，没有上传或构建入口，也没有门户授权链接的登录、到期和撤销功能；能够访问服务入口的人可查询导入的成果。需要这些授权控制时用主平台分享。

校验：python -m unittest tests.test_corpus_delivery。源码打包见 ../scripts/maintenance/README.md。
