# 跨电脑共享与只读 API

网页和 API 共用同一套已发布 Corpus Release，不必把网页改成另一套系统。分享链接限定一个已发布版本，持有链接的人可以只读检索、查询标注与下载术语频次，不能上传、修改来源或启动加工。撤销分享会阻止后续访问。

## 同一局域网

1. 在服务主机运行 `ipconfig`，找到对方电脑可访问的 IPv4 地址，例如 `192.168.1.20`。不要选择虚拟网卡，也不要把 `0.0.0.0` 当成访问地址。
2. 在项目根目录 `.env` 中设置以下字段，保留其他配置与密钥：

```dotenv
API_HOST=0.0.0.0
API_PORT=8000
PORTAL_PUBLIC_URL=http://192.168.1.20:8000
```

3. 等全部加工任务结束后，关闭原启动窗口，再启动 `python -m corpus_platform serve`。不要同时启动两个平台。Windows 防火墙只给需要的专用网络开放 TCP 8000；先确认电脑属于可信网络，再由管理员设置允许范围。
4. 在另一台电脑打开 `http://192.168.1.20:8000/health`，能返回正常状态后，再使用平台生成的分享链接。服务主机需要保持运行并避免休眠。

`127.0.0.1` 和 `localhost` 只表示访问者自己的电脑，不能发给其他电脑使用。分享窗口会提示当前公开地址、监听地址及本机候选局域网地址；候选地址不保证跨网段可达。既有分享路径与令牌仍然有效，但对外使用时需要换成新的主机地址，建议从当前平台重新生成并复制链接。

## 不在同一网络

将完整源码、对应数据库配置与语料发布资产部署到能被访问的服务器，使用域名和 HTTPS 反向代理，按实际域名设置 `PORTAL_PUBLIC_URL=https://corpus.example.org`。已有 `deploy/` 目录保留了部署配置。服务只开放必要的 Web 入口，数据库和内部 worker 保持内网访问。当前完整 BCC 原生索引链路优先使用已经验证的 Windows x64 环境；Linux/Docker 仍需单独验证。

此修改不会自动开放本机公网、防火墙或路由器端口。电脑之间无法建立网络连接时，改成 API 同样无法访问。

## 使用分享 API

先在所有者页面选择一个已发布版本，生成分享链接。假设网页链接为：

```text
http://192.168.1.20:8000/share/SHARE_TOKEN/search
```

API 基址就是去掉末尾的 `/search`：

```text
http://192.168.1.20:8000/share/SHARE_TOKEN
```

分享窗口同时提供这个 API 基址和 `/docs` 文档链接；机器可读描述是 `/openapi.json`。所有路径都加在同一个基址后面：

| 路径 | 功能 |
| --- | --- |
| `/api/v1/kwic?q=电池&limit=10&page=0` | 查询例句、所属话题名称、术语标记和来源 |
| `/api/v1/annotations?page=1&page_size=20` | 已保存逐句术语、话题 ID、路径和名称 |
| `/api/v1/terms?page_size=10` | 默认前十术语频次 |
| `/api/v1/terms/export` | 下载当前范围完整术语频次 CSV |
| `/api/v1/topics` | 正式体系、定义、标志词和标注统计 |
| `/api/v1/sources` | 来源类别与具体教材目录 |

检索和标注统计可增加 `source_category=教材&book_title=具体书名`；不填表示不限制。`q` 可用于术语筛选。CSV 下载沿用页面当前来源和术语筛选，包含所有匹配术语，不只前十。工作区 `dataset=workspace` 不向分享访问者开放。

```python
import requests

base = "http://192.168.1.20:8000/share/SHARE_TOKEN"
response = requests.get(
    base + "/api/v1/kwic",
    params={"q": "电池", "limit": 10, "page": 0, "source_category": "教材", "book_title": "具体书名"},
    timeout=30,
)
response.raise_for_status()
for row in response.json()["result"]["Context"]:
    print(row.get("TopicNames", []), row.get("Terms", []), row.get("Context"))

response = requests.get(base + "/api/v1/terms/export", timeout=60)
response.raise_for_status()
with open("term_frequency.csv", "wb") as stream:
    stream.write(response.content)
```

分享令牌相当于此版本的访问凭据，不要提交到 GitHub。Python、curl 等直接调用不需要浏览器跨域设置；从另一个网站的 JavaScript 调用时，还需要针对该站点配置跨域策略，不能为此开放构建写接口。

## 来源与话题修改后如何分享

在“基础语料文件”卡片内为每批选择报刊、新闻、论文、政策文件、教材或未分类，选择文件后点击“上传本批语料”。不同类别分批上传，每批上传后重新选择类别。选择教材时，每个 TXT 的文件名（不含扩展名）就是书名；同一 ZIP 内各 TXT 分别取自己的文件名，不取 ZIP 名。不同教材可以同批上传。现有语料在检索页切换“当前项目已保存标注”，展开“登记语料来源与具体教材”，直接勾选文档批量登记，也可下载、填写、上传 CSV。系统不会凭文件名把普通书籍猜成教材。

来源登记保存在 `inputs/metadata/upload_sources.csv`（上传文件对应）和 `inputs/metadata/sources.csv`（按已处理文档补录）。它们独立于原始语料文件；后者优先覆盖前者。发布时随该版本冻结，不会改变旧版本或旧分享。

检索页只显示话题名称。标志词在“领域话题体系”详情中查看。BERTopic 测试体系原本没有正式名称，会显示“待命名聚类”，不会把标志词冒充正式话题名。复用其他项目已批准的正式 `taxonomy.json` 时，可在上传后点击“直接复用已批准参考体系（不聚类）”，再从已发布话题体系继续全量分类。必须完成重新分类、结构化与新版本发布，旧测试版分享才有一个可替代的新正式分享；不能仅更换页面文字而保留错误的语句归类。

### 在网页设置监听与分享地址

使用已授权账号登录“我的语料库”，点击右上角“访问与分享设置”；分享检索窗口也提供同名按钮。可填写 API_HOST、API_PORT、PORTAL_PUBLIC_URL，页面分别显示当前生效值与已保存值。保存只更新项目根目录 `.env` 的这三个字段，保留数据库密码、模型密钥和其他配置，并在 `logs/network-settings-backups/` 保存原配置备份；该目录含私有配置，不能分享或提交Git。

保存不会立即改变监听、分享链接或停止任务。等加工结束后，从原启动窗口关闭平台再正常启动；重新复制分享链接。监听0.0.0.0允许网络连接，但对外URL必须使用对方实际可访问的IP/域名，不能写0.0.0.0，不能包含 `/share/...` 路径。防火墙、校园网隔离和服务器反向代理仍需由管理员按部署环境设置。系统/启动器环境变量若有同名配置，其优先级高于.env。

首次授权由主机维护者在项目根目录运行 `python -m corpus_platform portal allow-network-admin 账号名`，重启后该账号可见设置入口。撤销时追加 `--remove` 并重启。名单存于 `PORTAL_NETWORK_ADMIN_USERS`，默认没有账号具备此权限；普通账号、未登录者与分享访问者均不可读写此设置。网页不能修改管理员名单。此授权仅管理访问与分享配置，不改变其他用户项目的所有权。
