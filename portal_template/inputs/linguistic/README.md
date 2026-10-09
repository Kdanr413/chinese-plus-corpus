# 分词与术语识别统一输入接口

请把分词和术语识别所需的人工参考资料统一放在本目录：

1. `domain_lexicon.csv`：需要整体保留的领域表达；
2. `terminology.csv`：已知规范术语、别名、英文名和缩略语；
3. `segmentation_notes.csv`：强制合并等特殊切分规则；
4. `jieba_user_dict.txt`：Jieba 用户词典，每行格式为“词语 词频 词性”，词频和词性可省略；不要写注释行；
5. `stop_terms.txt`：每行一个不应成为候选术语的词或片段；
6. `general_reference_frequency.csv`：可选的通用中文参考词频，用于计算领域特异度。

分词器采用项目内封装的 Jieba 0.42.1。前三个受控文件和 Jieba 用户词典均可在操作台查看、更新并选择本轮是否使用。替换文件后不要修改文件名；如需改变路径，在 `configs/pipeline.json` 的 `linguistic` 节统一配置。
