# 完整源码发行维护

个人和服务器统一使用 GitHub 源码。最终源码包不附带 Python 便携环境或实际语料。

在项目根目录执行：

```powershell
python scripts/maintenance/build_source_release.py --output deliveries/NEW_RELEASE
```

输出必须是不存在的新目录。生成 corpus-source/ 完整干净源码、corpus-source.zip、SHA256SUMS.txt 和脱敏校验记录。代码、当前文档、输入格式、必要资源、测试和干净新项目模板均包含；真实 data、.env、账号数据库、密钥、缓存、运行环境、Git 历史不包含。输入词表复制为空接口，正式动态体系复制为空树。

脚本只读当前源文件，写入新发行目录，不删除原目录、不改 data/raw，也不自动推送 GitHub。验证后将 corpus-source/ 同步仓库，再将 ZIP 和校验文件作为对应提交的 Release 资产上传。旧发行目录清理应单独核对路径，用户原始文件和正式版本不得列为清理目标。
