import argparse
import csv
import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path


ANALYSIS_DETAIL = Path(r"D:\production\corpus\analysis\corpus_inventory_details.csv")
OUT_ROOT = Path(r"D:\production\corpus\processed")
OUT_JSONL = OUT_ROOT / "jsonl" / "documents.jsonl"
OUT_SECTIONS_JSONL = OUT_ROOT / "jsonl" / "sections.jsonl"
OUT_PARAGRAPHS_JSONL = OUT_ROOT / "jsonl" / "paragraphs.jsonl"
OUT_SENTENCES_JSONL = OUT_ROOT / "jsonl" / "sentences.jsonl"
OUT_CLEAN_TEXT_DIR = OUT_ROOT / "clean_text_by_source"
OUT_META = OUT_ROOT / "metadata" / "documents.csv"
OUT_REPORT = OUT_ROOT / "quality" / "cleaning_report.json"

USABLE_QUALITIES = {"完整可用", "可用短文本"}


def configure_paths(inventory_path: Path, output_root: Path):
    global ANALYSIS_DETAIL
    global OUT_ROOT
    global OUT_JSONL
    global OUT_SECTIONS_JSONL
    global OUT_PARAGRAPHS_JSONL
    global OUT_SENTENCES_JSONL
    global OUT_CLEAN_TEXT_DIR
    global OUT_META
    global OUT_REPORT

    ANALYSIS_DETAIL = inventory_path
    OUT_ROOT = output_root
    OUT_JSONL = OUT_ROOT / "jsonl" / "documents.jsonl"
    OUT_SECTIONS_JSONL = OUT_ROOT / "jsonl" / "sections.jsonl"
    OUT_PARAGRAPHS_JSONL = OUT_ROOT / "jsonl" / "paragraphs.jsonl"
    OUT_SENTENCES_JSONL = OUT_ROOT / "jsonl" / "sentences.jsonl"
    OUT_CLEAN_TEXT_DIR = OUT_ROOT / "clean_text_by_source"
    OUT_META = OUT_ROOT / "metadata" / "documents.csv"
    OUT_REPORT = OUT_ROOT / "quality" / "cleaning_report.json"


def parse_args():
    parser = argparse.ArgumentParser(
        description="清洗领域语料并生成文档、章节、段落和句子级 JSONL。"
    )
    parser.add_argument(
        "--inventory",
        type=Path,
        default=ANALYSIS_DETAIL,
        help="语料清单 corpus_inventory_details.csv",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=OUT_ROOT,
        help="处理结果根目录",
    )
    return parser.parse_args()

BOILERPLATE_PATTERNS = [
    r"^\s*当前位置[:：].*$",
    r"^\s*首页\s*[>＞].*$",
    r"^\s*上一篇[:：].*$",
    r"^\s*下一篇[:：].*$",
    r"^\s*分享到[:：]?.*$",
    r"^\s*打印\s*(本页)?\s*$",
    r"^\s*关闭\s*(窗口)?\s*$",
    r"^\s*网站地图\s*$",
    r"^\s*联系我们\s*$",
    r"^\s*友情链接\s*$",
    r"^\s*版权所有.*$",
    r"^.*ICP备案.*$",
    r"^.*公网安备.*$",
    r"^\s*主办单位[:：].*$",
    r"^\s*技术支持[:：].*$",
    r"^\s*\[?邮箱登[录陆]\]?\s*$",
    r"^\s*\|?\s*旧版网站\s*$",
    r"^\s*\|?\s*English\s*$",
    r"^\s*@[\w.-]+\.[A-Za-z]{2,}\s*$",
    r"^\s*(简|繁|EN)\s*$",
    r"^\s*(登录|退出|个人中心|邮箱|无障碍)\s*$",
    r"^\s*(打印|收藏|留言|关闭|返回顶部)\s*$",
    r"^\s*字号[:：]?(默认|大|超大)?\s*$",
    r"^\s*(正文|详细|【详细】|更多|查看更多\s*>?)\s*$",
    r"^\s*(前一个|后一个)[:：]\s*(无)?\s*$",
    r"^\s*(上一篇|下一篇)[:：]?\s*(无)?\s*$",
    r"^\s*(左右滑动)?查看更多\s*$",
    r"^\s*点击(下方)?[“\"]?阅读原文[”\"]?.*$",
    r"^\s*(练习方式|练习模式|答题模式|顺序练习|随机练习|章节练习|模拟考试|开始练习|我要练习)\s*$",
    r"^\s*服务热线[:：].*$",
    r"^\s*服务投诉[:：].*$",
    r"^\s*网站标识码[:：]?.*$",
    r"^\s*\d{4}\s*-\s*.*版权所有.*$",
]

PAGE_MARK_PATTERNS = [
    r"^\s*第?\s*\d+\s*[页頁]\s*$",
    r"^\s*[-—–]\s*\d+\s*[-—–]\s*$",
    r"^\s*\d+\s*/\s*\d+\s*$",
    r"^\s*Page\s+\d+(\s+of\s+\d+)?\s*$",
]

SOURCE_LINE_PATTERNS = [
    r"^\s*来源[:：].{0,80}$",
    r"^\s*发布时间[:：].{0,80}$",
    r"^\s*发布日期[:：].{0,80}$",
    r"^\s*作者[:：].{0,80}$",
    r"^\s*编辑[:：].{0,80}$",
    r"^\s*URL\s*:\s*https?://.*$",
    r"^\s*TITLE\s*:\s*.*$",
    r"^\s*DATE\s*:\s*.*$",
    r"^\s*标题[:：].{0,120}$",
    r"^\s*许可[:：].{0,120}$",
    r"^\s*索\s*引\s*号[:：]?\s*$",
    r"^\s*主题分类[:：]?\s*$",
    r"^\s*发文机关[:：]?\s*$",
    r"^\s*成文日期[:：]?\s*$",
    r"^\s*发文字号[:：]?\s*$",
    r"^\s*发布日期[:：]?\s*$",
]

CRAWLER_STOP_MARKERS = {
    "相关文章",
    "相关阅读",
    "相关链接",
    "最新新闻",
    "友情链接",
    "采购头条",
    "政采动态",
    "购买服务",
    "版权所有",
    "前一个：",
    "前一个:",
}

EXERCISE_HEADING_PATTERNS = [
    r"^(?:课后)?练习(?:题)?$",
    r"^习题(?:\s*\d+)?$",
    r"^复习题$",
    r"^思考题$",
    r"^自测题$",
    r"^测试题$",
    r"^问答题$",
    r"^名词解释$",
    r"^填空题$",
    r"^判断题$",
    r"^选择题$",
    r"^综合题$",
    r"^简答题$",
    r"^计算题$",
    r"^实训(?:任务|项目|练习)?$",
    r"^学习活动$",
]

DOMAIN_KEYWORDS = [
    "新能源汽车",
    "电动汽车",
    "纯电动",
    "混合动力",
    "插电式",
    "燃料电池汽车",
    "动力电池",
    "动力蓄电池",
    "电池管理系统",
    "驱动电机",
    "电驱动",
    "充电桩",
    "充电设施",
    "充换电",
    "换电站",
    "车网互动",
    "智能网联汽车",
    "自动驾驶汽车",
    "光伏",
    "风电",
    "储能",
    "氢能",
    "可再生能源",
    "新型电力系统",
    "能源转型",
]

CRAWLER_GENERIC_PAGE_TITLES = {
    "中国汽车工业协会分支机构介绍",
    "中国汽车工业协会",
    "中国充电联盟 | EVCIPA",
    "中华人民共和国工业和信息化部",
    "电动智能",
    "行业政策",
    "行业动态",
    "协会工作",
    "标准法规",
    "会员专区",
    "普通会员单位",
    "数据统计",
    "工信数据",
    "新闻发布会",
    "联盟工作-中国充电联盟 | EVCIPA",
    "会员服务-中国充电联盟 | EVCIPA",
    "关于联盟-中国充电联盟 | EVCIPA",
    "通知公告-中国充电联盟 | EVCIPA",
    "行业新闻-中国充电联盟 | EVCIPA",
}


def read_text(path: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1"):
        try:
            return path.read_text(encoding=enc, errors="strict")
        except Exception:
            continue
    return path.read_text(encoding="utf-8", errors="ignore")


def normalize_width(text: str) -> str:
    table = str.maketrans(
        {
            "\u3000": " ",
            "（": "(",
            "）": ")",
            "【": "[",
            "】": "]",
            "《": "<",
            "》": ">",
        }
    )
    return text.translate(table)


def normalize_line(line: str) -> str:
    line = normalize_width(line)
    line = re.sub(r"[\u200b\ufeff\xa0]", " ", line)
    line = re.sub(r"[ \t]+", " ", line)
    return line.strip()


def is_noise_line(line: str) -> bool:
    if not line:
        return False
    for pattern in BOILERPLATE_PATTERNS + PAGE_MARK_PATTERNS + SOURCE_LINE_PATTERNS:
        if re.match(pattern, line, flags=re.IGNORECASE):
            return True
    if len(line) <= 30 and re.fullmatch(r"[\d\s·\-—–_]+", line):
        return True
    return False


def heading_level_and_title(paragraph: str):
    text = paragraph.strip()
    if not text or len(text) > 100:
        return None
    if re.search(r"[。！？!?；;]$", text):
        return None
    if len(re.findall(r"[。！？!?；;]", text)) > 0:
        return None
    if re.fullmatch(r"\d{4}[./-]\d{1,2}[./-]\d{1,2}(?:\s+\d{1,2}:\d{2})?", text):
        return None
    if re.fullmatch(r"[\d\s./:%％-]+", text):
        return None
    if re.search(r"/\s*\d+\s*$", text):
        return None

    chapter_match = re.match(
        r"^第[一二三四五六七八九十百\d]+[章篇部分]\s*(.*)$", text
    )
    if chapter_match and len(chapter_match.group(1).strip()) >= 2:
        return 1, text, "chapter", 0.98
    if re.match(r"^附录(?:\s*[A-ZＡ-Ｚ一二三四五六七八九十\d]+)?(?:\s+|$)", text):
        return 1, text, "appendix", 0.95
    section_match = re.match(r"^第[一二三四五六七八九十百\d]+节\s*(.*)$", text)
    if section_match and len(section_match.group(1).strip()) >= 2:
        return 2, text, "section", 0.96

    article_match = re.match(
        r"^第[一二三四五六七八九十百\d]+条(?:\s*[:：]?\s*)(.*)$", text
    )
    if article_match:
        remainder = article_match.group(1).strip()
        if not remainder or (len(remainder) <= 28 and not re.search(r"[，,：:]", remainder)):
            return 2, text, "legal_article", 0.90
        return None

    if re.match(r"^[一二三四五六七八九十]+[、.．]\s*\S", text):
        if len(text) <= 70:
            return 2, text, "chinese_numbered", 0.88
        return None

    dot_match = re.match(r"^(\d{1,3}(?:\.\d{1,3}){0,3})[、.．]?\s+(\S.+)$", text)
    if dot_match:
        numbering = dot_match.group(1)
        depth = dot_match.group(1).count(".") + 1
        title_part = dot_match.group(2).strip()
        digit_groups = re.findall(r"\d+(?:\.\d+)?", text)
        language_chars = len(re.findall(r"[\u4e00-\u9fffA-Za-z]", title_part))
        if "." not in numbering:
            return None
        if (
            len(title_part) <= 60
            and language_chars >= 4
            and len(digit_groups) <= 2
            and re.match(r"^[\u4e00-\u9fffA-Za-z]", title_part)
            and not re.search(r"[与和及的为对在由]$", title_part)
            and not re.search(r"[，,]|[…]{2,}|[.．]{5,}|[%％~～]", title_part)
        ):
            return min(depth + 1, 4), text, "arabic_numbered", 0.84
        return None

    if re.match(r"^[（(][一二三四五六七八九十]+[）)]\s*\S", text):
        body_part = re.sub(r"^[（(][一二三四五六七八九十]+[）)]\s*", "", text)
        if (
            len(text) <= 45
            and not re.search(r"[=:：]", body_part)
            and not (
                len(body_part) > 28
                and re.search(r"(?:的是|采用|提高|求得|可以|进行|已经|需要|能够|分为)", body_part)
            )
        ):
            return 3, text, "parenthesized_numbered", 0.82
        return None
    return None


def is_heading_line(line: str) -> bool:
    return heading_level_and_title(line) is not None


def extract_header_metadata(raw: str, fallback_title: str = ""):
    title_match = re.search(r"^\s*TITLE\s*:\s*(.+?)\s*$", raw, flags=re.IGNORECASE | re.MULTILINE)
    url_match = re.search(r"^\s*URL\s*:\s*(.+?)\s*$", raw, flags=re.IGNORECASE | re.MULTILINE)
    date_match = re.search(r"^\s*DATE\s*:\s*(.+?)\s*$", raw, flags=re.IGNORECASE | re.MULTILINE)
    title = normalize_line(title_match.group(1)) if title_match else fallback_title
    title = re.sub(r"_(?:中国政府网|中国政府采购网|工业和信息化部)$", "", title).strip()
    return {
        "title": title or fallback_title,
        "url": normalize_line(url_match.group(1)) if url_match else "",
        "date": normalize_line(date_match.group(1)) if date_match else "",
    }


def select_crawler_main_content(raw: str, source: str, title: str) -> str:
    lines = [normalize_line(line) for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    lines = [
        line
        for line in lines
        if not re.match(r"^(?:URL|TITLE|DATE)\s*:", line, flags=re.IGNORECASE)
    ]
    nonempty = [index for index, line in enumerate(lines) if line]
    if not nonempty:
        return ""

    start = nonempty[0]
    search_limit = min(len(lines), 140)
    normalized_title = re.sub(r"\s+", "", title)

    if source == "caam":
        body_markers = [i for i in range(search_limit) if lines[i] == "正文"]
        if body_markers:
            start = body_markers[-1] + 1

    if start == nonempty[0] and normalized_title:
        title_hits = []
        for i in range(search_limit):
            candidate = re.sub(r"\s+", "", lines[i])
            if candidate and (
                candidate == normalized_title
                or normalized_title.startswith(candidate) and len(candidate) >= 12
                or candidate.startswith(normalized_title) and len(normalized_title) >= 12
            ):
                title_hits.append(i)
        if title_hits:
            # Crawled pages commonly repeat the title once after navigation.
            # A later repetition may be a "related article" link, so never use
            # the last occurrence as the body start.
            start = (title_hits[1] if len(title_hits) >= 2 else title_hits[0]) + 1

    if source == "govcn_policy":
        release_markers = [
            i
            for i in range(search_limit)
            if re.match(r"^发布日期[:：]?", lines[i])
        ]
        if release_markers:
            start = max(start, release_markers[-1] + 1)

    selected = []
    accumulated_chars = 0
    for line in lines[start:]:
        marker = line.strip()
        if accumulated_chars >= 10 and (
            marker in CRAWLER_STOP_MARKERS
            or marker.startswith("前一个：")
            or marker.startswith("主办单位：")
        ):
            break
        selected.append(line)
        accumulated_chars += len(line)
    return "\n".join(selected)


def crawler_domain_relevance(title: str, text: str):
    title_lower = title.lower()
    body = text[:12000].lower()
    title_terms = [term for term in DOMAIN_KEYWORDS if term.lower() in title_lower]
    body_counts = {term: body.count(term.lower()) for term in DOMAIN_KEYWORDS if term.lower() in body}
    total_hits = sum(body_counts.values())
    unique_hits = len(body_counts)
    body_chars = max(len(normalized_hash_text(body)), 1)
    density_per_1000 = total_hits * 1000 / body_chars
    relevant = (
        bool(title_terms)
        or (unique_hits >= 2 and total_hits >= 4 and density_per_1000 >= 0.8)
        or (unique_hits == 1 and total_hits >= 5 and density_per_1000 >= 3.0)
    )
    return {
        "relevant": relevant,
        "title_terms": title_terms,
        "body_terms": sorted(body_counts, key=lambda term: body_counts[term], reverse=True),
        "total_hits": total_hits,
        "unique_hits": unique_hits,
        "density_per_1000": round(density_per_1000, 3),
    }


def is_exercise_heading(text: str) -> bool:
    value = normalize_line(text)
    value = re.sub(
        r"^(?:第[一二三四五六七八九十百\d]+[章节]\s*|"
        r"[一二三四五六七八九十]+[、.．]\s*|"
        r"\d+(?:\.\d+){0,3}[、.．]?\s*)",
        "",
        value,
    ).strip()
    return any(re.fullmatch(pattern, value) for pattern in EXERCISE_HEADING_PATTERNS)


def remove_exercise_blocks(paragraphs):
    kept = []
    skip_level = None
    removed = 0
    for paragraph in paragraphs:
        heading = heading_level_and_title(paragraph)
        if is_exercise_heading(paragraph):
            skip_level = heading[0] if heading else 4
            removed += 1
            continue
        if skip_level is not None:
            if heading and heading[0] <= skip_level:
                skip_level = None
            else:
                removed += 1
                continue
        kept.append(paragraph)
    return kept, removed


def remove_repeated_running_headers(paragraphs, window: int = 25):
    kept = []
    last_seen = {}
    removed = 0
    for paragraph in paragraphs:
        heading = heading_level_and_title(paragraph)
        if heading and heading[2] in {"chapter", "section"}:
            key = normalized_hash_text(heading[1])
            current_position = len(kept)
            if key in last_seen and current_position - last_seen[key] <= window:
                removed += 1
                continue
            last_seen[key] = current_position
        kept.append(paragraph)
    return kept, removed


def should_join(prev: str, curr: str) -> bool:
    if not prev or not curr:
        return False
    if is_heading_line(prev) or is_heading_line(curr):
        return False
    if re.search(r"[。！？；:：.!?;:]$", prev):
        return False
    if re.match(r"^([（(]?\d+[）)]|[一二三四五六七八九十]+[、.．]|[-•●])", curr):
        return False
    if len(prev) < 8:
        return False
    return True


def split_paragraphs(text: str):
    paragraphs = []
    for block in re.split(r"\n{2,}", text.strip()):
        block = re.sub(r"[ \t]+", " ", block).strip()
        if block:
            paragraphs.append(block)
    return paragraphs


def split_sentences(paragraph: str):
    paragraph = paragraph.strip()
    if not paragraph:
        return []
    if is_heading_line(paragraph):
        return [paragraph]

    parts = re.findall(r".+?(?:[。！？!?；;]+[”’）)]*|$)", paragraph)
    sentences = []
    for part in parts:
        sentence = part.strip()
        if not sentence:
            continue
        sentences.append(sentence)
    return sentences or [paragraph]


def safe_filename(value: str, max_len: int = 90) -> str:
    value = re.sub(r"[\\/:*?\"<>|]+", "_", value)
    value = re.sub(r"\s+", "_", value).strip("._ ")
    if not value:
        value = "untitled"
    return value[:max_len]


def clean_text(raw: str, source: str = "", crawler: bool = False, title: str = "") -> str:
    if crawler:
        raw = select_crawler_main_content(raw, source=source, title=title)
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    raw = re.sub(r"^\s*URL\s*:\s*https?://[^\n]*\n?", "", raw, flags=re.IGNORECASE | re.MULTILINE)
    raw = re.sub(r"^\s*TITLE\s*:\s*[^\n]*\n?", "", raw, flags=re.IGNORECASE | re.MULTILINE)
    raw = re.sub(r"^\s*DATE\s*:\s*[^\n]*\n?", "", raw, flags=re.IGNORECASE | re.MULTILINE)
    raw = re.sub(r"^标题[:：][^\n]*\n?", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"^来源[:：]https?://[^\n]*\n?", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"^许可[:：][^\n]*\n?", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\n{3,}", "\n\n", raw)

    cleaned_lines = []
    for raw_line in raw.split("\n"):
        line = normalize_line(raw_line)
        if is_noise_line(line):
            continue
        if line in CRAWLER_STOP_MARKERS:
            continue
        cleaned_lines.append(line)

    paragraphs = []
    current = ""
    for line in cleaned_lines:
        if not line:
            if current:
                paragraphs.append(current.strip())
                current = ""
            continue
        if not current:
            current = line
        elif should_join(current, line):
            current += line
        else:
            paragraphs.append(current.strip())
            current = line
    if current:
        paragraphs.append(current.strip())

    text = "\n\n".join(p for p in paragraphs if p)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def looks_like_site_index(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if "计时中首页关于联盟会员服务联盟工作" in compact:
        return True
    if compact.startswith("新闻发布会新闻发布新闻发布会") or "国新办新闻发布会国务院政策例行吹风会部新闻发布会其他新闻发布会" in compact:
        return True
    if len(compact) < 1200 and sum(compact.count(p) for p in ["首页", "关于", "会员服务", "通知公告", "行业新闻", "更多"]) >= 5:
        return True
    if len(compact) < 1200 and sum(compact.count(p) for p in ["上一页", "下一页", "当前位置", "网站地图", "联系我们"]) >= 3:
        return True
    return False


def normalized_hash_text(text: str) -> str:
    compact = re.sub(r"\s+", "", text)
    compact = re.sub(r"[^\w\u4e00-\u9fff]+", "", compact, flags=re.UNICODE)
    return compact.lower()


def infer_domain(category: str, source: str, title: str) -> str:
    text = f"{category} {source} {title}"
    if "新能源汽车" in text or "电动汽车" in text or "车辆" in text:
        return "新能源汽车"
    if any(k in text for k in ["能源", "光伏", "风电", "储能", "电力"]):
        return "新能源"
    if "政策" in text or "白皮书" in text:
        return "政策法规"
    return "新能源及新能源汽车"


def infer_initial_topics(title: str, category: str, text: str):
    sample = f"{title}\n{category}\n{text[:5000]}"
    topic_keywords = {
        "动力电池": ["动力电池", "锂离子电池", "磷酸铁锂", "三元电池", "电池管理", "BMS", "热失控"],
        "驱动电机": ["驱动电机", "永磁同步", "异步电机", "电机控制", "电驱动"],
        "电控系统": ["整车控制", "电控", "控制器", "VCU", "MCU"],
        "充换电基础设施": ["充电", "换电", "充电桩", "充电站", "充换电"],
        "燃料电池": ["燃料电池", "氢燃料", "质子交换膜", "氢能"],
        "混合动力": ["混合动力", "插电式", "PHEV", "增程"],
        "智能网联": ["智能网联", "自动驾驶", "车联网", "辅助驾驶"],
        "政策法规": ["政策", "规划", "通知", "意见", "标准", "法规"],
        "产业市场": ["销量", "市场", "产业", "企业", "产销", "出口"],
        "储能": ["储能", "电化学储能", "储能系统"],
        "光伏": ["光伏", "太阳能", "太阳电池"],
        "风电": ["风电", "风能", "风力发电"],
    }
    hits = []
    for topic, keywords in topic_keywords.items():
        score = sum(sample.count(k) for k in keywords)
        if score > 0:
            hits.append((score, topic))
    hits.sort(reverse=True)
    return [topic for _, topic in hits[:5]]


def quality_summary(text: str):
    stripped = text.strip()
    norm = normalized_hash_text(stripped)
    chars = len(norm)
    cn_chars = len(re.findall(r"[\u4e00-\u9fff]", stripped))
    cn_ratio = cn_chars / max(chars, 1)
    replacement_ratio = stripped.count("\ufffd") / max(len(stripped), 1)
    nav_hits = sum(stripped.count(p) for p in ["首页", "上一页", "下一页", "当前", "联系我们", "网站地图", "分享", "打印"])
    nav_density = nav_hits / max(chars / 1000, 1)

    if chars < 500:
        return "不完整/过短", "文本量低于500字"
    if replacement_ratio > 0.015:
        return "不完整/乱码较多", "替换字符比例偏高"
    if nav_hits >= 6 and (chars < 2500 or nav_density >= 4):
        return "纯网站介绍/导航噪声", "导航与站点模板词密度较高"
    if chars < 1200:
        return "可用短文本", "文本较短，适合短篇政策/新闻语料"
    if chars < 3000:
        return "可用短文本", "文本低于3000字，适合作为短文语料"
    if cn_ratio < 0.05 and chars < 5000:
        return "需复核", "中文比例低且文本不长"
    return "完整可用", "正文规模充足"


def source_root(path: Path) -> str:
    try:
        path.relative_to(Path(r"D:\production\corpus\book"))
        return "book"
    except ValueError:
        return "crawler"


def rel_path(path: Path) -> str:
    root = Path(r"D:\production\corpus\book") if source_root(path) == "book" else Path(r"D:\production\corpus\crawler")
    return str(path.relative_to(root))


def path_parts_lower(path: Path):
    return [p.lower() for p in path.parts]


def is_candidate(path: Path):
    if path.suffix.lower() != ".txt":
        return False, "non_text_suffix"
    parts_lower = path_parts_lower(path)
    excluded_parts = {
        "__pycache__",
        "logs",
        "raw_html",
        "scripts",
        "configs",
        "metadata",
        "pages",
        "txt_search_pages",
    }
    if any(part in excluded_parts for part in parts_lower):
        return False, "non_corpus_directory"
    name_lower = path.name.lower()
    excluded_name_patterns = [
        "readme",
        "copyright",
        "manifest",
        "download_results",
        "results",
        "scan",
        "metadata",
        "未下载说明",
        "processing_summary",
    ]
    if any(pat in name_lower for pat in excluded_name_patterns):
        return False, "metadata_or_readme"
    if any(pat in name_lower for pat in ["combined_", "_combined", "合集"]):
        return False, "combined_duplicate"
    return True, ""


def priority_and_category(path: Path):
    parts = path_parts_lower(path)
    root = source_root(path)
    if root == "crawler":
        if "data_nev" in parts and "txt" in parts:
            source = next((p for p in path.parts if p.lower() in {"caam", "ccgp", "evcipa", "govcn_policy", "miit"}), "crawler_txt")
            return 80, "爬虫抽取正文", source
        return 30, "爬虫其他文本", "crawler_other"

    if "terminology_extracted" in parts:
        return 95, "教材术语表/词表", "new_energy_vehicle_book"
    if "txt_reflowed" in parts:
        return 100, "新能源汽车教材", "new_energy_vehicle_book"
    if "new_energy_vehicle_textbook_zh_simplified" in parts:
        return 92, "新能源汽车开放教材/百科", "open_textbook_simplified"
    if "new_energy_zh_simplified" in parts:
        return 91, "新能源开放教材/百科", "open_textbook_simplified"
    if "ocr_txt" in parts:
        return 60, "OCR教材文本", "new_energy_vehicle_book"
    if "txt" in parts and "new_energy_vehicle_book" in parts:
        return 70, "教材抽取文本", "new_energy_vehicle_book"
    if "eng" in parts:
        return 78, "英文权威报告/教材", "international_reports"
    if len(Path(rel_path(path)).parts) == 1:
        return 85, "中文政策/白皮书/权威报告", "official_reports"
    return 75, "其他书籍/报告文本", "book_other"


def family_key(path: Path):
    parts = path_parts_lower(path)
    stem = path.stem.lower()
    stem = re.sub(r"[_\-\s]*(reflowed|ocr|txt)$", "", stem)
    if "new_energy_vehicle_book" in parts:
        return f"nev_book::{stem}"
    if "new_energy_vehicle_textbook_zh_simplified" in parts or "new_energy_zh_simplified" in parts:
        return f"open_textbook::{stem}"
    return ""


def load_inventory():
    with ANALYSIS_DETAIL.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    usable = [
        row
        for row in rows
        if row.get("dedup_status") == "kept" and row.get("quality") in USABLE_QUALITIES
    ]
    usable.sort(key=lambda r: (r["root"], r["category"], r["source"], r["relative_path"]))
    return usable


def make_doc_id(index: int, path: str, text_hash: str) -> str:
    stem = hashlib.sha1(path.encode("utf-8")).hexdigest()[:8]
    return f"doc_{index:05d}_{stem}_{text_hash[:8]}"


def make_section_id(doc_id: str, index: int) -> str:
    return f"{doc_id}__sec_{index:04d}"


def make_paragraph_id(doc_id: str, index: int) -> str:
    return f"{doc_id}__p_{index:05d}"


def make_sentence_id(doc_id: str, index: int) -> str:
    return f"{doc_id}__s_{index:06d}"


def parse_structure(doc_id: str, title: str, paragraphs):
    sections = []
    paragraph_rows = []
    section_counter = 0
    current_stack = []
    heading_path_stack = []
    root_id = make_section_id(doc_id, section_counter)
    root_section = {
        "section_id": root_id,
        "parent_section_id": "",
        "level": 0,
        "section_type": "document_root",
        "heading": title or "文档",
        "heading_path": title or "文档",
        "start_paragraph_index": 0,
        "end_paragraph_index": len(paragraphs) - 1 if paragraphs else -1,
        "paragraph_count": len(paragraphs),
        "text": "",
        "text_hash": "",
        "synthetic": True,
        "heading_rule": "document_root",
        "heading_confidence": 1.0,
    }
    sections.append(root_section)
    current_stack.append(root_section)
    heading_path_stack.append(title or "文档")

    for idx, para in enumerate(paragraphs):
        heading_info = heading_level_and_title(para)
        if heading_info:
            level, heading, heading_rule, heading_confidence = heading_info
            while len(current_stack) > 1 and current_stack[-1]["level"] >= level:
                current_stack[-1]["end_paragraph_index"] = idx - 1
                current_stack.pop()
                if heading_path_stack:
                    heading_path_stack.pop()
            parent_section = current_stack[-1]
            section_counter += 1
            section_id = make_section_id(doc_id, section_counter)
            path_list = [s["heading"] for s in current_stack[1:]] + [heading]
            section = {
                "section_id": section_id,
                "parent_section_id": parent_section["section_id"],
                "level": level,
                "section_type": "heading",
                "heading": heading,
                "heading_path": " > ".join(path_list),
                "start_paragraph_index": idx,
                "end_paragraph_index": idx,
                "paragraph_count": 1,
                "text": "",
                "text_hash": "",
                "synthetic": False,
                "heading_rule": heading_rule,
                "heading_confidence": heading_confidence,
            }
            sections.append(section)
            current_stack.append(section)
            heading_path_stack.append(heading)
            paragraph_rows.append(
                {
                    "doc_id": doc_id,
                    "paragraph_id": make_paragraph_id(doc_id, idx + 1),
                    "paragraph_index": idx,
                    "section_id": section_id,
                    "section_level": level,
                    "section_path": section["heading_path"],
                    "is_heading": True,
                    "heading_rule": heading_rule,
                    "heading_confidence": heading_confidence,
                    "text": para,
                    "text_hash": hashlib.sha256(normalized_hash_text(para).encode("utf-8")).hexdigest(),
                    "char_count": len(para),
                }
            )
        else:
            current_section = current_stack[-1]
            paragraph_rows.append(
                {
                    "doc_id": doc_id,
                    "paragraph_id": make_paragraph_id(doc_id, idx + 1),
                    "paragraph_index": idx,
                    "section_id": current_section["section_id"],
                    "section_level": current_section["level"],
                    "section_path": current_section["heading_path"],
                    "is_heading": False,
                    "heading_rule": "",
                    "heading_confidence": 0.0,
                    "text": para,
                    "text_hash": hashlib.sha256(normalized_hash_text(para).encode("utf-8")).hexdigest(),
                    "char_count": len(para),
                }
            )

        for sec in current_stack:
            sec["end_paragraph_index"] = idx

    for sec in sections:
        if paragraphs and sec["end_paragraph_index"] < sec["start_paragraph_index"]:
            sec["end_paragraph_index"] = sec["start_paragraph_index"]
        if sec["end_paragraph_index"] >= sec["start_paragraph_index"]:
            sec["paragraph_count"] = sec["end_paragraph_index"] - sec["start_paragraph_index"] + 1
        else:
            sec["paragraph_count"] = 0

    return sections, paragraph_rows


def enrich_section_text(sections, paragraphs):
    for sec in sections:
        if sec["end_paragraph_index"] < 0:
            sec["text"] = ""
            sec["text_hash"] = ""
            continue
        block = paragraphs[sec["start_paragraph_index"] : sec["end_paragraph_index"] + 1]
        sec_text = "\n\n".join(block)
        sec["text"] = sec_text
        sec["text_hash"] = hashlib.sha256(normalized_hash_text(sec_text).encode("utf-8")).hexdigest()
    return sections


def build_sentence_rows(doc_id: str, paragraph_rows):
    sentence_rows = []
    sentence_index = 0
    for para in paragraph_rows:
        sentences = split_sentences(para["text"])
        for sentence_index_in_paragraph, sentence in enumerate(sentences):
            sentence_rows.append(
                {
                    "doc_id": doc_id,
                    "sentence_id": make_sentence_id(doc_id, sentence_index + 1),
                    "sentence_index": sentence_index,
                    "sentence_index_in_paragraph": sentence_index_in_paragraph,
                    "paragraph_id": para["paragraph_id"],
                    "paragraph_index": para["paragraph_index"],
                    "section_id": para["section_id"],
                    "section_level": para["section_level"],
                    "section_path": para["section_path"],
                    "is_heading": para["is_heading"],
                    "text": sentence,
                    "text_hash": hashlib.sha256(normalized_hash_text(sentence).encode("utf-8")).hexdigest(),
                    "char_count": len(sentence),
                }
            )
            sentence_index += 1
    return sentence_rows


def write_clean_source_text(doc):
    source_type_dir = OUT_CLEAN_TEXT_DIR / safe_filename(doc["source_type"])
    source_dir = source_type_dir / safe_filename(doc["source"])
    source_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{doc['doc_id']}__{safe_filename(doc['title'])}.txt"
    path = source_dir / filename
    header = [
        f"title: {doc['title']}",
        f"doc_id: {doc['doc_id']}",
        f"domain: {doc['domain']}",
        f"topics_auto: {'；'.join(doc['topics_auto'])}",
        f"source_type: {doc['source_type']}",
        f"source: {doc['source']}",
        f"source_url: {doc.get('source_url', '')}",
        f"source_date: {doc.get('source_date', '')}",
        f"quality: {doc['quality']}",
        f"paragraph_count: {doc['paragraph_count']}",
        f"section_count: {doc['section_count']}",
        f"sentence_count: {doc['sentence_count']}",
        f"original_path: {doc['original_path']}",
        "",
        doc["text"],
        "",
    ]
    path.write_text("\n".join(header), encoding="utf-8")
    return str(path)


def main():
    # This directory is fully generated by this script. Recreate it on every
    # full run so documents rejected by newer rules cannot leave stale TXT files.
    if OUT_CLEAN_TEXT_DIR.exists():
        if OUT_CLEAN_TEXT_DIR.name != "clean_text_by_source":
            raise RuntimeError(f"Refusing to clear unexpected output directory: {OUT_CLEAN_TEXT_DIR}")
        shutil.rmtree(OUT_CLEAN_TEXT_DIR)

    out_dirs = [
        OUT_JSONL.parent,
        OUT_SECTIONS_JSONL.parent,
        OUT_PARAGRAPHS_JSONL.parent,
        OUT_SENTENCES_JSONL.parent,
        OUT_CLEAN_TEXT_DIR,
        OUT_META.parent,
        OUT_REPORT.parent,
    ]
    for out_dir in out_dirs:
        out_dir.mkdir(parents=True, exist_ok=True)

    rows = load_inventory()
    docs = []
    skipped = []
    seen_hashes = {}
    total_sections = 0
    total_paragraphs = 0
    total_sentences = 0
    total_exercise_paragraphs_removed = 0
    total_running_headers_removed = 0

    for idx, row in enumerate(rows, start=1):
        path = Path(row["path"])
        try:
            raw = read_text(path)
        except Exception as exc:
            skipped.append({"path": row["path"], "reason": f"read_error:{type(exc).__name__}"})
            continue

        metadata = extract_header_metadata(raw, fallback_title=path.stem)
        title = metadata["title"]
        is_crawler_source = source_root(path) == "crawler"
        if is_crawler_source and title in CRAWLER_GENERIC_PAGE_TITLES:
            skipped.append(
                {
                    "path": row["path"],
                    "reason": "crawler_list_or_home_page",
                    "title": title,
                }
            )
            continue
        cleaned = clean_text(
            raw,
            source=row["source"],
            crawler=is_crawler_source,
            title=title,
        )
        if looks_like_site_index(cleaned):
            norm = normalized_hash_text(cleaned)
            skipped.append({"path": row["path"], "reason": "site_index_after_cleaning", "norm_chars": len(norm)})
            continue

        paragraphs = split_paragraphs(cleaned)
        paragraphs, exercise_removed = remove_exercise_blocks(paragraphs)
        paragraphs, running_headers_removed = remove_repeated_running_headers(paragraphs)
        total_exercise_paragraphs_removed += exercise_removed
        total_running_headers_removed += running_headers_removed
        cleaned = "\n\n".join(paragraphs).strip()
        norm = normalized_hash_text(cleaned)
        if len(norm) < 500:
            skipped.append({"path": row["path"], "reason": "too_short_after_cleaning", "norm_chars": len(norm)})
            continue

        relevance = crawler_domain_relevance(title, cleaned) if is_crawler_source else None
        if relevance and not relevance["relevant"]:
            skipped.append(
                {
                    "path": row["path"],
                    "reason": "out_of_domain_after_cleaning",
                    "title": title,
                    "domain_evidence": relevance,
                    "norm_chars": len(norm),
                }
            )
            continue

        text_hash = hashlib.sha256(norm.encode("utf-8")).hexdigest()
        if text_hash in seen_hashes:
            skipped.append(
                {
                    "path": row["path"],
                    "reason": "duplicate_after_cleaning",
                    "duplicate_of": seen_hashes[text_hash],
                    "norm_chars": len(norm),
                }
            )
            continue
        seen_hashes[text_hash] = row["path"]

        sections, paragraph_rows = parse_structure(
            doc_id=make_doc_id(idx, row["path"], text_hash),
            title=title,
            paragraphs=paragraphs,
        )
        sections = enrich_section_text(sections, paragraphs)

        # Update paragraph rows with hashes after paragraph list has been finalized.
        for para_row in paragraph_rows:
            para_row["text_hash"] = hashlib.sha256(normalized_hash_text(para_row["text"]).encode("utf-8")).hexdigest()

        doc_id = sections[0]["section_id"].split("__sec_")[0]
        sentence_rows = build_sentence_rows(doc_id, paragraph_rows)
        doc = {
            "doc_id": doc_id,
            "title": title,
            "domain": infer_domain(row["category"], row["source"], title),
            "topics_auto": infer_initial_topics(title, row["category"], cleaned),
            "source_type": row["category"],
            "source": row["source"],
            "source_url": metadata["url"],
            "source_date": metadata["date"],
            "quality": row["quality"],
            "root": row["root"],
            "original_path": row["path"],
            "relative_path": row["relative_path"],
            "language": "zh",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "raw_chars": len(raw),
            "clean_chars": len(cleaned),
            "norm_chars": len(norm),
            "paragraph_count": len(paragraphs),
            "section_count": len(sections),
            "sentence_count": len(sentence_rows),
            "exercise_paragraphs_removed": exercise_removed,
            "running_headers_removed": running_headers_removed,
            "domain_relevance": relevance,
            "text_hash": text_hash,
            "text": cleaned,
        }
        doc["clean_text_path"] = write_clean_source_text(doc)
        docs.append(doc)
        total_sections += len(sections)
        total_paragraphs += len(paragraph_rows)
        total_sentences += len(sentence_rows)

        row["_doc"] = doc
        row["_sections"] = sections
        row["_paragraphs"] = paragraph_rows
        row["_sentences"] = sentence_rows

    # Write document-level corpus.
    with OUT_JSONL.open("w", encoding="utf-8", newline="\n") as f:
        for doc in docs:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")

    # Write section-level corpus.
    with OUT_SECTIONS_JSONL.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            sections = row.get("_sections")
            if not sections:
                continue
            doc = row["_doc"]
            for sec in sections:
                sec_out = {
                    "doc_id": doc["doc_id"],
                    "section_id": sec["section_id"],
                    "parent_section_id": sec["parent_section_id"],
                    "level": sec["level"],
                    "section_type": sec["section_type"],
                    "heading_rule": sec["heading_rule"],
                    "heading_confidence": sec["heading_confidence"],
                    "heading": sec["heading"],
                    "heading_path": sec["heading_path"],
                    "start_paragraph_index": sec["start_paragraph_index"],
                    "end_paragraph_index": sec["end_paragraph_index"],
                    "paragraph_count": sec["paragraph_count"],
                    "text_hash": sec["text_hash"],
                    "text": sec["text"],
                    "source_type": doc["source_type"],
                    "source": doc["source"],
                    "quality": doc["quality"],
                    "relative_path": doc["relative_path"],
                }
                f.write(json.dumps(sec_out, ensure_ascii=False) + "\n")

    # Write paragraph-level corpus.
    with OUT_PARAGRAPHS_JSONL.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            paragraphs = row.get("_paragraphs")
            if not paragraphs:
                continue
            doc = row["_doc"]
            for para in paragraphs:
                para_out = {
                    "doc_id": doc["doc_id"],
                    "paragraph_id": para["paragraph_id"],
                    "paragraph_index": para["paragraph_index"],
                    "section_id": para["section_id"],
                    "section_level": para["section_level"],
                    "section_path": para["section_path"],
                    "is_heading": para["is_heading"],
                    "heading_rule": para["heading_rule"],
                    "heading_confidence": para["heading_confidence"],
                    "text_hash": para["text_hash"],
                    "text": para["text"],
                    "char_count": para["char_count"],
                    "source_type": doc["source_type"],
                    "source": doc["source"],
                    "quality": doc["quality"],
                    "relative_path": doc["relative_path"],
                }
                f.write(json.dumps(para_out, ensure_ascii=False) + "\n")

    # Write sentence-level corpus.
    with OUT_SENTENCES_JSONL.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            sentences = row.get("_sentences")
            if not sentences:
                continue
            doc = row["_doc"]
            for sent in sentences:
                sent_out = {
                    "doc_id": doc["doc_id"],
                    "sentence_id": sent["sentence_id"],
                    "sentence_index": sent["sentence_index"],
                    "sentence_index_in_paragraph": sent["sentence_index_in_paragraph"],
                    "paragraph_id": sent["paragraph_id"],
                    "paragraph_index": sent["paragraph_index"],
                    "section_id": sent["section_id"],
                    "section_level": sent["section_level"],
                    "section_path": sent["section_path"],
                    "is_heading": sent["is_heading"],
                    "text_hash": sent["text_hash"],
                    "text": sent["text"],
                    "char_count": sent["char_count"],
                    "source_type": doc["source_type"],
                    "source": doc["source"],
                    "quality": doc["quality"],
                    "relative_path": doc["relative_path"],
                }
                f.write(json.dumps(sent_out, ensure_ascii=False) + "\n")

    meta_fields = [
        "doc_id",
        "title",
        "domain",
        "topics_auto",
        "source_type",
        "source",
        "source_url",
        "source_date",
        "quality",
        "root",
        "raw_chars",
        "clean_chars",
        "norm_chars",
        "paragraph_count",
        "section_count",
        "sentence_count",
        "exercise_paragraphs_removed",
        "running_headers_removed",
        "text_hash",
        "clean_text_path",
        "original_path",
        "relative_path",
    ]
    with OUT_META.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=meta_fields)
        writer.writeheader()
        for doc in docs:
            row = {field: doc.get(field, "") for field in meta_fields}
            row["topics_auto"] = "；".join(doc["topics_auto"])
            writer.writerow(row)

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_inventory": str(ANALYSIS_DETAIL),
        "output_documents_jsonl": str(OUT_JSONL),
        "output_sections_jsonl": str(OUT_SECTIONS_JSONL),
        "output_paragraphs_jsonl": str(OUT_PARAGRAPHS_JSONL),
        "output_sentences_jsonl": str(OUT_SENTENCES_JSONL),
        "output_clean_text_by_source": str(OUT_CLEAN_TEXT_DIR),
        "output_metadata": str(OUT_META),
        "usable_inventory_rows": len(rows),
        "documents_written": len(docs),
        "sections_written": total_sections,
        "paragraphs_written": total_paragraphs,
        "sentences_written": total_sentences,
        "exercise_paragraphs_removed": total_exercise_paragraphs_removed,
        "running_headers_removed": total_running_headers_removed,
        "skipped": skipped,
        "total_clean_chars": sum(doc["clean_chars"] for doc in docs),
        "total_norm_chars": sum(doc["norm_chars"] for doc in docs),
    }
    OUT_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    args = parse_args()
    configure_paths(args.inventory, args.output_root)
    main()
