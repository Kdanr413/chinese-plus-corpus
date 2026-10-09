# 共享发行包维护

在主项目目录执行：

```powershell
python scripts/maintenance/build_share_packages.py --output deliveries/NEW_RELEASE --runtime deliveries/2026-10-09/01-builder/runtime-win64.zip
```

NEW_RELEASE 必须是不存在的目录。工具复制当前源码、清空输入接口中的私人内容、生成空动态话题体系和干净模板，重建三个独立 README、ZIP、隐私检查和 SHA256 校验值。核心运行环境沿用现有 Windows 包，不下载依赖或模型。

结果验证后再替换共享目录，禁止把正在使用的 data/portal、.env、个人 Git 历史或真实成果混入源码包。工具不会删除原目录，也不会推送 GitHub。02-corpus-data 默认空，需要分享成果时先导出完整正式版本，再独立打包。
