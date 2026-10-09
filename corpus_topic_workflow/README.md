# corpus_topic_workflow

负责建立并发布三级话题体系，不负责术语识别。

## 输入

- 清洗后的全部有效句子或完整对话话轮；
- 固定样本量 `ceil(有效单元数 / 3)`；
- 可选参考体系 `configs/topics/new_energy_candidate_taxonomy.json`；
- 页面临时提供的一个或多个模型配置。

## 过程与输出

任务文件保存在 `data/workspaces/chinese-plus/topic-workflow/`，包括候选、模型独立标注、冲突和人工裁决。发布后的正式体系写入：

```text
data/curated/topics/taxonomy.json
```

主流程收到发布消息后，调用 `corpus_recognition` 对全量单元进行话题分类。不要把临时聚类 ID 当作正式话题 ID。

## BERTopic 测试直发

聚类完成后可调用 `POST /api/workflow/rounds/{round_id}/publish-bertopic-test`，请求体必须包含 `{"confirm_test_mode": true}`。系统排除 `BT_OUTLIER`，把其余簇转换成三级测试路径并激活到平台；轮次文件写入 `publish_mode=bertopic_test` 和 `test_only=true`。本模式不写入 canonical taxonomy，只用于测试后续链路。

当工作台由完整加工流程打开时，主页面通过发布消息和状态轮询两条通道恢复任务，随后由 `corpus_recognition` 使用当前体系分类全量分析单元。独立打开话题工作台时没有待续跑的主任务，发布后需回到首页启动完整流程。
