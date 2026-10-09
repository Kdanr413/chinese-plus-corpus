# collect

`web.py` 是唯一网页采集实现，读取 `configs/collect/websites.json`，将原始响应保存到 `data/raw/web/`。默认治理流程只盘点，不联网；必须显式选择采集才会执行。新增网站时只增加配置和对应解析规则，并保留来源 URL、采集时间和权利状态。
