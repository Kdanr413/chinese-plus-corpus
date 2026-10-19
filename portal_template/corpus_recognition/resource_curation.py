"""Auditable, offline energy-resource curation. Never overwrites input assets."""
from __future__ import annotations
import argparse,csv,json,re,hashlib,unicodedata
from collections import Counter
from pathlib import Path

# Explicit conservative domain rules; extraction scores are never treated as probabilities.
TECHNICAL = set("光伏 风电 氢能 储能 新能源 可再生能源 非化石能源 生物质 生物柴油 生物乙醇 沼气 太阳能 地热 潮汐 潮流能 波浪能 核能 核电 水电 水能 燃料电池 动力电池 锂离子 钠离子 铅酸 电化学 电解 源网荷储 微电网 智能电网 虚拟电厂 特高压 直流电网 电力市场 绿证 绿电 碳交易 碳捕集 碳封存 碳排放 碳中和 碳达峰 光热 风能 海洋能 热泵 光电 光生 光吸收 光催化 半导体 晶硅 多晶硅 单晶硅 非晶硅 钙钛矿 碲化镉 薄膜电池 光伏组件 光伏硅 硅片 硅材料 硅太阳能 锂电池 液流电池 电池储能 超级电容 飞轮 飞轮储能 压缩空气 抽水蓄能 熔盐 燃烧 热解 炭化 生物炭 气化 氢气 储氢 制氢 氢燃料 氢脆 氢储能 氢化物 催化剂 电极 电解质 电解液 电池 负极 正极 隔膜 质子交换膜 锂硫 磷酸铁锂 磷酸锰铁锂 磷酸钒锂 镍氢 镍镉 锂钴 锂锰 锂镍 储热 余热 逆变器 变流器 变压器 变电站 配电网 输电网 输电线 并网 充电 换电 电动汽车 电动车 混合动力 增程式 燃油汽车 内燃机 发动机 电机 电驱动 传动系 电力系统 功率因数 调峰 调频 电能质量 无功 电压稳定 电网稳定 黑启动 能量管理 能源互联网 能源管理 综合能源 能源转型 能源效率 清洁能源 化石能源 天然气 煤炭 煤电 页岩气 可燃冰 温室气体 二氧化碳 二氧化硫 氮氧化物 甲烷 乙醇 甲醇 氧化还原 水合物 电荷 载流子 晶界 晶格 晶体缺陷 量子点 能带 禁带 费米能级 霍尔效应 空穴 少子 掺杂 复合速率 电导 电阻率 迁移率 热导 热电 压电 电介质 永磁 同步电动机 异步电动机 风力 风轮 风机 叶轮 风速 电力电子 电容 电感 碳纤维 石墨 石墨烯 纳米硅 氢氧化 氧化锂 氧化镍 氧化钴 碳酸锂 硫化锂 三元材料 有机光伏 聚合物电池 染料敏化".split())
EXACT = set("PN结 p-n结 p型 n型 BMS EMS SOC SOH SOFC PEMFC PAFC MCFC AFC DMFC DSSC CIGS CdTe CCUS CCS V2G MPPT PWM DC/DC IGBT MOSFET eVTOL 电池 电极 电解质 电解液 隔膜 光伏 风电 氢能 储能 沼气 风能 核能 核电 水电 水能 地热 绿电 绿证 生物质 甲烷 甲醇 乙醇 空穴 少子 掺杂 晶界 晶格 晶面 晶胞 晶粒 氧化还原 钙钛矿 超导体 光伏效应 霍尔效应 光电效应 热岛效应 飞轮 逆变器 变流器 整流器 变压器 电动机 发电机 电容器 电感器 电阻器 半导体 石墨烯 碳纳米管 电导率 电阻率 光子 质子 正极 负极 电荷 氢脆 调峰 调频 黑启动 电压 电流 功率 能量密度 比容量 比能量 倍率性能 循环寿命 库仑效率 开路电压 短路电流 填充因子 转换效率 迁移率 燃料电池 动力电池 光伏发电 风力发电".split())
GENERIC = set("技术 系统 研究 发展 利用 应用 分析 方法 设备 装置 材料 结构 性能 能量 能源 中国 世界 北京 企业 公司 产业 领域 生产 产品 经济 市场 需求 服务 用户 进行 提高 降低 减少 实现 影响 因此 小时 时间 方面 问题 情况 工作 结果 过程 数量 能力 水平 就业 数据 信息 模型 参数 实验 条件 效率 温度 压力 质量 安全 价格 成本 作用 变化 资源 会议 政策 规划 目标 要求 建设 项目 管理 标准 创新 国家 国际 人民 甲方 乙方 不可抗力 财务 净利润 同比增长 营业收入 院士 专家 发布 示范 统计 方案 通知 办法 目录 文件 报告 年度 工作人员 消费者".split())
ENDINGS = tuple("电池 能源 储能 发电 供热 制冷 制氢 储氢 输氢 加氢 氢能 光伏 风电 充电 换电 能量 管理 系统 材料 器件 设备 装置 技术 工艺 过程 参数 效应 机制 模式 方法 控制 调度 优化 规划 交易 市场 政策 规则 标准 规范 建设 消纳 并网 输电 配电 传输 电网 电站 场站 组件 硅片 薄膜 晶体 晶界 缺陷 掺杂 载流子 空穴 浓度 密度 强度 电压 电流 功率 能效 效率 容量 速率 寿命 性能 特性 稳定性 安全性 可靠性 电极 电解质 电解液 膜 隔膜 催化剂 电容 电感 电阻 逆变器 变流器 电动机 发电机 发动机 汽车 电动车 机组 风机 叶片 风轮 风速 风场 热泵 污染 排放 减排 中和 达峰 碳汇 捕集 封存 回收 利用 转换 转化 合成 分解 热解 气化 燃烧 炭化 电解 产氢 储热 传热 温度 电池组 电池包 产业 产业链 供应链 成本 价格 电价 补贴 绿证 绿电 化合物 氧化物 氢化物 硫化物 碳化物 碳酸盐 微电网 配电网 热电联产 分布式能源 燃料 桩 站 逆变 保护 监测 检测 测试 试验 计量 结算 保护器 继电器 传感器 电路 变换器 变换 运动 运行 结构 反应 动力 热值 热量 网络 并联 串联 串并联 荷电状态 放电 充放电 自放电 动力学 风险 产量 资源 储量 弃风 弃光 限电 调节 循环 活性 比 电池片 能 器 率 度 系数 电量 硅 烷 醇 酸 盐 酯 碳 气 镍 钴 锂 氢 氧 镉 镁 锌 钠".split())
FRAGMENT = re.compile(r"主要|由于|能够|可以|这些|一种|以及|通过|采用|进行|对于|其中|上述|需要|包括|超过|不足|大约|左右|尽可能|有利于|取决于|具有|所示|所述|而言|等方面|的发展|的应用|的影响|了|着")

PREFIX_EXCEPTIONS = set("建筑 压缩 压电 掺杂 增程 超导 受控 先进 可控 可充 可再生 可持续 共享 互补 混合 复合 联合 分布式 集中式 直接 间接 并联 串联 串并联 交直流 直流 交流 双向 双馈 变速 变频 变桨 恒速 恒频 多晶 单晶 非晶 柔性 刚性 多孔 多结 单结 叠层 本征 硅基 薄膜 纳米 纳米晶 量子 无机 有机 介孔 宽禁带 窄禁带 高温 低温 常温 室温 高压 低压 高功率 高能量 高容量 高效率 高性能 高比能 高比容量 高温熔盐 超高压 超低温 新型 小型 大型 微型 海上 陆上 车载 户用 园区 电网侧 用户侧 发电侧 金属 锂金属 金属锌 金属空气 铁铬 全钒 全固态 半固态 凝胶 聚合物 固态 固体 液态 液体 气态 气体 超临界 非水 水系 水性 水溶性 异质 同质 异步 同步 永磁 电励磁 无刷 有刷 起动 启动 浮动 漂浮式 垂直轴 水平轴 非并网 离网 独立 分散式 生物 秸秆 木质 纤维素 非粮 多能 多能源 低碳 零碳 清洁 绿色 传统 常规 退役 梯次 阶梯 单 双 多 三 四 一级 二级 三级 第一代 第二代 第三代 N P n p L N型 P型 n型 p型".split())
def sha256(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def normalized(value):return unicodedata.normalize('NFKC',value).strip()
def decision(term,keywords):
    if term in GENERIC:return 'reject','完全通用词'
    if len(term)<2 or len(term)>28 or not re.fullmatch(r"[A-Za-z0-9一-鿿α-ωΑ-Ω+()/·.%-]+",term):return 'reject','非完整词形/标点/过长片段'
    if term.startswith(('.', '-', '/', '%')) or re.match(r'^\d+(?:[.、章节点])',term) or re.fullmatch(r"[0-9.]+[A-Za-z%]*|[IVX]+|[A-Z]\.",term):return 'reject','数值单位或编号'
    if term in EXACT:return 'keep','人工明确保留的领域或支撑技术词'
    if FRAGMENT.search(term) or re.match(r'^(?:现有|各种|各类|很多|少量|大量|一个|多个|某种|部分|新型的|增加|减少|提高|降低|推动|加快|发展|中国的)',term):return 'reject','含句法连接成分/描述性片段'
    evidence=sorted(x for x in TECHNICAL if x in term)
    if not evidence:return 'review','领域属性证据不足（不进入正式标注表）'
    if re.search(r'的|为|是|将|把|从|向|对|指|获得|利用|实现',term) or re.search(r'作为|是指|称为|成为|分为|用于|基于|相比|相关|有关|熟悉|培育|上升|下降|不断|基本|水平|主要|重要|某一|每个|一款|到电|及其|和|以及',term):return 'reject','句法残片或描述性词组'
    if re.match(r'^(?:中国|我国|全球|世界|全国|美国|德国|日本|法国|英国|欧盟|特斯拉|比亚迪|宁德时代|大众|丰田|宝马|奔驰|一汽|上汽|小米|小鹏|华为|第|图|表|章|节|\d)',term):return 'review','地域/品牌/编号/数量修饰，不作为通用标注术语'
    first=min(term.find(x) for x in evidence);prefix=term[:first]
    if prefix and prefix not in PREFIX_EXCEPTIONS:return 'review','前置修饰不在已确认技术构词表中，需确认完整词形'
    if len(term)>20:return 'review','过长复合词，需人工确认边界'
    if term in keywords:return 'keep' ,'正式话题标志词且含领域词素'
    if term.endswith(ENDINGS):return 'keep','领域词素+完整术语结尾'
    return 'review','存在领域词素但词形需复核'

def write_csv(path,rows,fields):
    with path.open('w',encoding='utf-8-sig',newline='') as h:
        writer=csv.DictWriter(h,fieldnames=fields,extrasaction='ignore');writer.writeheader();writer.writerows(rows)

def curate(terms,taxonomy,annotations,output):
    output=Path(output)
    if output.exists():raise FileExistsError('Output exists; never overwrite a prior curation')
    original=json.loads(Path(taxonomy).read_text(encoding='utf-8-sig'));nodes=original['nodes'];seen=set();fixed=[];mapping=[]
    for raw in nodes:
        n=dict(raw);parts=[normalized(x) for x in str(n['path']).split('>') if x.strip()]
        if not 1<=len(parts)<=3:raise ValueError('Taxonomy depth must be 1..3')
        path=' > '.join(parts)
        if path in seen:raise ValueError('Duplicate topic path: '+path)
        seen.add(path);n.update(path=path,name=parts[-1],level=len(parts));n['keywords']=list(dict.fromkeys(normalized(x) for x in n.get('keywords',[]) if normalized(x)));fixed.append(n)
    by_path={n['path']:n for n in fixed};ids=set()
    for n in fixed:
        if n['id'] in ids:raise ValueError('Duplicate topic ID')
        ids.add(n['id']);parts=n['path'].split(' > ');parent=' > '.join(parts[:-1])
        if parent and parent not in by_path:raise ValueError('Missing parent')
        n['parent_id']=by_path[parent]['id'] if parent else None
        if not n.get('definition'):n['definition']=f"{n['name']}相关的领域对象及分类主题。"
        n['classification_rule']='优先匹配定义与标志词；仅在三级叶节点分类；按句子主旨选择最具体话题，不因单个普通词命中而分类。'
    for n in sorted(fixed,key=lambda n:-n['level']):
        if not n['keywords']:
            children=[c for c in fixed if c['parent_id']==n['id']]
            n['keywords']=list(dict.fromkeys([n['name']]+[w for c in children for w in c['keywords']]))[:15]
            n['keywords_source']='由本级名称及直接子级已存标志词汇总'
        else:n['keywords_source']='保留原正式体系标志词'
    fixed.sort(key=lambda n:n['path'])
    frequency=Counter();sentence_frequency=Counter();sentence_count=0
    with Path(annotations).open(encoding='utf-8-sig') as h:
        for line in h:
            if not line.strip():continue
            row=json.loads(line);sentence_count+=1;text=str(row.get('clean_text',row.get('text','')))
            for word in set(x for x in row.get('terms',[]) if isinstance(x,str)):
                frequency[word]+=text.count(word);sentence_frequency[word]+=1
    keywords={w for n in fixed for w in n['keywords']};kept=[];rejected=[];review=[];names=set();input_count=0
    with Path(terms).open(encoding='utf-8-sig',newline='') as h:
        for raw in csv.DictReader(h):
            input_count+=1;word=normalized(raw['preferred_term']);action,reason=decision(word,keywords)
            # Normalization cannot silently change surface matching in already saved sentences.
            if word!=raw['preferred_term']:action,reason='review','NFKC或空白规范化改变词形，需确认原文匹配'
            if word in names:action,reason='reject','规范化后重复术语'
            if action=='keep':names.add(word)
            item=dict(raw,preferred_term=word,aliases='',english_name='',abbreviation='',priority='100',notes=reason,reference_score=raw.get('score',''),score_source='原基础提取表score；仅作参考，非术语正确概率',frequency=frequency[raw['preferred_term']],sentence_frequency=sentence_frequency[raw['preferred_term']],domain_evidence=';'.join(sorted(x for x in TECHNICAL if x in word)),retention_reason=reason,status='approved' if action=='keep' else 'needs_review' if action=='review' else 'excluded')
            {'keep':kept,'reject':rejected,'review':review}[action].append(item)
    kept.sort(key=lambda r:(-float(r['score'] or 0),-r['frequency'],r['preferred_term']))
    output.mkdir(parents=True)
    fields=['term_id','preferred_term','aliases','english_name','abbreviation','status','priority','notes','score','reference_score','score_source','frequency','sentence_frequency','domain_evidence','retention_reason']
    for name,rows in [('terminology.csv',kept),('excluded_terms.csv',rejected),('review_terms.csv',review)]:write_csv(output/name,rows,fields)
    normalized_taxonomy=dict(original,taxonomy_version='4-normalized',normalization_base_version=original.get('taxonomy_version'),nodes=fixed,normalization_status='structural_normalization_not_model_reclassification')
    (output/'taxonomy.json').write_text(json.dumps(normalized_taxonomy,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (output/'reference_taxonomy.json').write_text(json.dumps(dict(normalized_taxonomy,classification_object='新能源及其边界领域中的句子主题'),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    write_csv(output/'topics.csv',[dict(n,level_1=n['path'].split(' > ')[0],level_2=(n['path'].split(' > ')+['',''])[1],level_3=(n['path'].split(' > ')+['',''])[2],keywords=';'.join(n['keywords'])) for n in fixed],['id','name','level','parent_id','level_1','level_2','level_3','path','definition','keywords','keywords_source','classification_rule'])
    write_csv(output/'topic_mapping.csv',[{'old_id':raw['id'],'new_id':raw['id'],'old_path':raw['path'],'new_path':next(n['path'] for n in fixed if n['id']==raw['id']),'action':'保留主题语义和编号；规范父子关系、格式与标志词'} for raw in nodes],['old_id','new_id','old_path','new_path','action'])
    leaves=[dict(n,ancestor_ids=[by_path[' > '.join(n['path'].split(' > ')[:depth])]['id'] for depth in (1,2)]) for n in fixed if n['level']==3]
    (output/'classification_topics.json').write_text(json.dumps({'taxonomy_version':'4-normalized','classification_target':'选择最具体的三级话题，一级和二级由父路径派生；不强迫无关句子进入体系。','topics':leaves},ensure_ascii=False,indent=2),encoding='utf-8')
    manifest={'input_terms':input_count,'kept_terms':len(kept),'excluded_terms':len(rejected),'review_terms':len(review),'sentence_count':sentence_count,'topic_levels':dict(Counter(n['level'] for n in fixed)),'keywords_missing':sum(not n['keywords'] for n in fixed),'source_sha256':{name:sha256(path) for name,path in [('terms',terms),('taxonomy',taxonomy),('annotations',annotations)]},'rules':'conservative-energy-v3','score_note':'保留原基础提取得分，不把1.0解释为正确率；未调用模型。','activation':'独立输出；未覆盖项目原术语、原体系、原标注或已发布Release。'}
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    return manifest

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('terms','taxonomy','annotations','output'):p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();print(json.dumps(curate(a.terms,a.taxonomy,a.annotations,a.output),ensure_ascii=False))
if __name__=='__main__':main()
