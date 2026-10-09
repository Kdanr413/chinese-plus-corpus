# llm config

`prompts.json` 保存页面中四类提示词模板和响应结构。API 地址、模型名和密钥不写入本文件；密钥只在当前运行进程中使用。页面保存新模板前会生成 `prompts.json.previous` 作为单层回退。
