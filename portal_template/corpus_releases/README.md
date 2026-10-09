# corpus_releases

管理可编辑 Workspace 和不可默认覆盖的 Corpus Release。

## 公共能力

- `create_workspace()`：创建可重复加工的工作区；
- `create_release()`：校验后冻结新版本；
- `validate_release()`：检查缺失、意外或被修改文件；
- `read_release()`：读取已验证的 Manifest；
- `create_workspace_from_release()`：基于旧版本开始新任务；
- `python -m corpus_releases ...`：命令行入口。

## 关键规则

- Workspace 位于 `data/workspaces/<corpus_id>/<workspace_id>/`；
- Release 位于 `data/releases/<corpus_id>/<version>/`；
- 相同版本禁止覆盖；
- `manifest.json` 是资产清单和 SHA-256 依据，不能手工修补来绕过错误；
- Release 需要修改时，必须建立新 Workspace 和新版本。

当前接口以本模块源码与 tests/ 中的回归检查为准；操作说明见主 README。
