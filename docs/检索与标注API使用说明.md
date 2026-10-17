# 检索、逐句标注与领域统计 API

## 1. 先确定地址与数据范围

个人和服务器使用相同代码。以下 `BASE` 是当前项目或分享入口地址：

- 登录后的项目：`http://127.0.0.1:8000/p/项目UUID`。外部脚本先登录统一平台，携带会话 Cookie；写操作还需 X-CSRF-Token。
- 只读分享：`https://你的域名/share/分享令牌`。仅能访问分享的正式版本，无需登录，禁止读取其他项目及 workspace。
- 单用户兼容服务：直接使用服务地址；workspace 接口需要管理员权限。配置 ADMIN_API_TOKEN 时传 X-Admin-Token，勿把管理员令牌交给检索访客。

`dataset=workspace`：当前项目已落盘标注，未建好索引也能调用；不等同于一个完整发布版。
`dataset=release`（默认）：所选语料库版本附带的冻结标注；传 `corpus_key`、`version`。旧版本没有附带标注时返回 available=false，不使用其他版本补齐。

所有 page 从1开始；只有 BCC `/kwic` 的 page 从0开始。所有查询只读，不会调用大模型。

## 2. 接口列表

| GET路径 | 内容 | 常用参数 |
| --- | --- | --- |
| /api/v1/annotations | 逐句/话轮正文、术语及编号、话题路径及编号、文档编号和标注运行来源 | dataset, sentence_id, term, topic, source_category, book_title, page, page_size（最多200） |
| /api/v1/terms | 领域术语频次、术语编号、标注单元数 | dataset, q, source_category, book_title, page, page_size（最多200） |
| /api/v1/topics | 完整话题节点、定义、版本号、来源轮次及标注数量 | dataset, source_category, book_title |
| /api/v1/sources | 来源目录、文档编号、类别、具体教材、发布版的BCC文档对应 | dataset, page, page_size（最多1000） |
| /api/v1/search | 现有BCC频率检索 | q, corpus_key, version, limit, source_category, book_title |
| /api/v1/kwic | 现有BCC上下文例句 | q, corpus_key, version, limit, page, window, source_category, book_title |

`term` 支持完整术语或术语编号；`topic` 支持完整路径、祖先路径或话题编号。`sentence_id` 是源句子ID；`unit_id` 是本轮识别单元ID；`analysis_unit_type` 区分句子和完整话轮。未保存编号时字段为空，不能推测编号。

`frequency` 是已被标注的术语在所属句子/话轮正文中的非重叠字面出现次数；`sentence_frequency` 是出现该标注的单元数。同一词不同时被多条标注重复计算，但不同术语间允许重叠。这不是对未标注位置的全库词频；后者使用BCC `/search`。

话题 `sentence_frequency` 是该路径直接标注数，`subtree_sentence_frequency` 是自身及下级标签次数之和；多标签时不是去重句子数。

KWIC保留BCC原始 `Source`、`Global`、`Group`，并附加 `Terms`、`TopicPaths`、`SourceCategory`、`BookTitle`。`annotations` 读取保存的语义标注；只有已建索引版本的KWIC才提供实际BCC标记，不把工作区结果伪装成索引返回。

## 3. Python：登录项目、查询标注及统计

```python
import getpass
import requests

host = "http://127.0.0.1:8000"   # 服务器部署后替换为自己的HTTPS域名
session = requests.Session()
login = session.post(host + "/_portal/api/login", json={
    "username": input("账号："), "password": getpass.getpass("密码：")
}, timeout=30)
login.raise_for_status()
csrf = login.json()["csrf_token"]
projects = session.get(host + "/_portal/api/projects", timeout=30)
projects.raise_for_status()
for project in projects.json()["items"]:
    print(project["id"], project["name"])
project_id = input("选择项目UUID：").strip()
base = host + "/p/" + project_id

r = session.get(base + "/api/v1/annotations", params={
    "dataset": "workspace", "term": "储能", "page": 1, "page_size": 20
}, timeout=180)
r.raise_for_status()
for sentence in r.json()["items"]:
    print(sentence["sentence_id"], sentence["text"], sentence["terms"], sentence["topics"])

r = session.get(base + "/api/v1/terms", params={
    "dataset": "workspace", "q": "储能", "page_size": 50
}, timeout=180)
r.raise_for_status()
print(r.json())

r = session.get(base + "/api/v1/topics", params={"dataset": "workspace"}, timeout=180)
r.raise_for_status()
print(r.json()["taxonomy_version"], r.json()["items"])
```

按源句子ID读取时，将 annotations 的 `term` 改成 `sentence_id`。所有分页返回 total；用 page、page_size 循环读取，不要一次请求所有正文。

## 4. 登记来源，教材精确到书名

来源为：报刊、新闻、论文、教材、未分类。它们是人工确认的来源类别，不根据文件名猜测。普通书籍/报告默认未分类。先 GET sources 取得文档ID，再调用：

```python
r = session.post(base + "/api/v1/source-metadata", headers={"X-CSRF-Token": csrf}, json={
    "items": [{"document_id": "替换为实际文档ID", "source_category": "教材", "book_title": "实际教材名·第一册"}]
}, timeout=30)
r.raise_for_status()
```

每批最多2000篇，同批文档ID不重复；教材书名必填，不能登记其他项目文档。只更新当前项目 `inputs/metadata/sources.csv`，已有发布版保持不变。检索页也提供来源CSV下载与回传；模板见 `参考文件中心/模板/语料来源登记.csv`。未来在服务器发布新版本时，来源与标注一起冻结到 Release 的 metadata/knowledge/。

## 5. 限制来源的BCC查询

```python
r = session.get(base + "/api/v1/kwic", params={
    "corpus_key": "chinese-plus", "version": "填写已完成索引的版本号",
    "q": "储能", "source_category": "教材", "book_title": "实际教材名·第一册",
    "limit": 20, "page": 0, "window": 50
}, timeout=180)
r.raise_for_status()
print(r.json())
```

`source_category=论文` 限制论文，空值表示全部来源；书名必须与来源登记完全一致。来源条件进入BCC已经验证的每文档范围索引，跨文档频率相加、语境统一分页；不是先查全库再过滤当前页。旧版本缺少 `scope_indexes.json` 时明确返回400，不会静默退回全库检索。没有匹配来源时结果为空。

此项目本机未完成结构化/索引时，可正常查 workspace 标注、术语频次和话题体系，但不能凭此执行BCC全文检索；请将结果与源码一起迁移到服务器完成发布。

## 6. 只读分享调用

将上面的 base 换成分享地址，使用 requests.get，无需登录；`dataset=release`，corpus_key 和 version 需是分享的版本。所有新增GET接口在分享入口只读开放；workspace 返回403，来源登记POST不可用。不要发送管理员令牌或数据库凭据。

## 7. 常见返回

- 200、available=false：版本没有附带该类标注，不代表有零条已标注数据。
- 400：来源/书名不合法、缺少已验证范围索引等；看 detail 或 error.message。
- 401/403：未登录、无管理权限，或尝试通过分享访问工作区。
- 404：语料库、版本或文档不属于当前作用域。
- 409/503：版本不可检索、索引或服务不可用。

主服务的 /docs 提供接口手册，/api-reference 是交互式API入口；脚本调用优先使用本说明中的完整项目地址。


## 术语前十、名称字段与跨电脑分享

`GET /api/v1/terms` 默认 `page_size=10`，仍可显式分页读取；`GET /api/v1/terms/export` 下载当前来源、书名及术语筛选范围的全部词频 CSV。两个接口使用同一统计口径。网页显示话题名称，KWIC 新增 `Topics`（含稳定ID、原路径、名称和待命名状态）与 `TopicNames`；原 `TopicPaths` 保留用于追溯，测试聚类不会伪装成正式领域名称。`GET /api/v1/topics` 提供体系节点的名称、定义、关键词和计数。

分享访问者将上述路径加到 `/share/SHARE_TOKEN` 后；`/docs` 和 `/openapi.json` 均只包含只读检索接口。不得请求工作区或来源写入接口。跨电脑监听与公开URL配置、请求示例见 [跨电脑共享与只读API](跨电脑共享与只读API.md)。

### 上传时按批登记来源

基础语料卡片先选本批类别，再选择 TXT/ZIP 并点击“上传本批语料”。不同类别分批上传。教材书名自动取每个 TXT 文件名（不含扩展名），ZIP 内也逐个 TXT 登记。上传 API `/api/upload?category=corpus&filename=教材甲.txt&source_category=教材` 可省略 `book_title`，服务端按文件名登记；显式传入 `book_title` 时仍作为该次上传的书名覆盖值。已有文档的来源修改接口仍须提供具体书名。来源表保存在当前项目 `inputs/metadata/upload_sources.csv`，原文不改写，旧发布版不变。
