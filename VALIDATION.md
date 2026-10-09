# 本次验证范围

- 现有 API、Release、检索和新增交付测试：62 项运行成功，其中 3 项原有可选测试跳过。
- 干净构建副本在临时迁移目录：便携 Python 的离线 doctor 和真实语料清洗成功。
- 已解包、重定位的便携环境：对数据包真实执行 BCC 词频和上下文查询，均 HTTP 200；查询后 Release 校验仍通过。
- 初始正式动态体系为空，发行包没有原作者输入、历史结果、个人 Git 历史或实际 .env。
- 没有在全新环境重跑完整 BERTopic、多模型审核与最终发布；使用者仍需首次安装话题依赖、下载模型、配置自己的 PostgreSQL 和外部模型账号。
- 没有开放公网、设置防火墙或验证实际外网连接；只读接口边界已经测试。
- 验证平台：Windows x64、Python 3.11.16。其他系统尚未验证。

源码检索服务边界回归：

```powershell
.\runtime\python.exe -m unittest tests.test_corpus_delivery
```
