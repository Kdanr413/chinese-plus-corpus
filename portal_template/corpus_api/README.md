# corpus_api

FastAPI 服务层和统一网页入口。

## 它做什么

- 提供操作台、话题体系弹窗和查询页面；
- 接收多文件上传并管理本轮选择；
- 调用 Pipeline、Recognition、Release、Registry 和 Search 公共接口；
- 从 PostgreSQL 找到 Release，再让 LangSC.BCC 读取文件系统索引；
- 提供 REST API 和 `/docs` 自动文档。

## 启动与输出

```powershell
python -m corpus_api
```

- 首页：`http://127.0.0.1:8000/`
- API 文档：`http://127.0.0.1:8000/docs`
- 页面静态文件：`corpus_api/web/`
- 上传原件：`data/raw/`
- 查询响应：JSON；浏览器可另存 CSV/JSON

数据库配置读取 `DB_*`，数据根目录读取 `CORPUS_DATA_ROOT`。本模块不保存语料正文到 PostgreSQL，也不复制 BCC 算法。


### 标注查询、领域统计与来源筛选

检索页增加领域术语频次、正式话题体系和来源筛选。已保存项目标注无需BCC索引即可查看；只读分享限定为所分享的发布版。支持报刊、新闻、论文、政策文件、教材（精确到书名），未确认来源保留为未分类。新增 annotations、terms、topics、sources API，完整登录、调用、来源登记示例见 [检索与标注API使用说明](../docs/检索与标注API使用说明.md)。旧版本缺少范围索引会明确拒绝来源限制，不回退成全库查询。
