import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BOOK_ROOT = PROJECT_ROOT / "data" / "raw" / "manual"
CRAWLER_ROOT = PROJECT_ROOT / "data" / "raw" / "web"
OUT_DIR = PROJECT_ROOT / "data" / "interim" / "inventory"
EXCLUSION_MANIFEST = PROJECT_ROOT / "data" / "raw" / "corpus_exclusions.csv"

TEXT_SUFFIXES = {".txt"}
EXCLUDED_PARTS = {
    "__pycache__",
    "logs",
    "raw_html",
    "scripts",
    "configs",
    "metadata",
    "pages",
    "txt_search_pages",
}
EXCLUDED_NAME_PATTERNS = [
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
COMBINED_PATTERNS = [
    "combined_",
    "_combined",
    "合集",
]

NAV_PHRASES = [
    "首页",
    "上一页",
    "下一页",
    "上一篇",
    "下一篇",
    "当前位置",
    "网站地图",
    "联系我们",
    "版权所有",
    "ICP备案",
    "分享到",
    "打印",
    "关闭",
    "相关链接",
    "友情链接",
    "主办单位",
    "技术支持",
]


def parse_args():
    parser = argparse.ArgumentParser(
        description="扫描手工文件与网站采集文本，生成去重和质量筛查清单。"
    )
    parser.add_argument("--manual-root", type=Path, default=BOOK_ROOT)
    parser.add_argument("--web-root", type=Path, default=CRAWLER_ROOT)
    parser.add_argument("--output-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--exclude-manifest", type=Path, default=EXCLUSION_MANIFEST)
    return parser.parse_args()


def configure_paths(manual_root: Path, web_root: Path, output_dir: Path):
    global BOOK_ROOT, CRAWLER_ROOT, OUT_DIR
    BOOK_ROOT = manual_root.resolve()
    CRAWLER_ROOT = web_root.resolve()
    OUT_DIR = output_dir.resolve()


def read_text(path: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1"):
        try:
            return path.read_text(encoding=enc, errors="strict")
        except Exception:
            continue
    return path.read_text(encoding="utf-8", errors="ignore")


def normalized_text(text: str) -> str:
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "", text, flags=re.UNICODE)
    return text.lower()


def chinese_count(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text))


def source_root(path: Path) -> str:
    try:
        path.relative_to(BOOK_ROOT)
        return "book"
    except ValueError:
        return "crawler"


def rel_path(path: Path) -> str:
    root = BOOK_ROOT if source_root(path) == "book" else CRAWLER_ROOT
    return str(path.relative_to(root))


def path_parts_lower(path: Path):
    return [p.lower() for p in path.parts]


def is_candidate(path: Path):
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return False, "non_text_suffix"
    parts_lower = path_parts_lower(path)
    if any(part in EXCLUDED_PARTS for part in parts_lower):
        return False, "non_corpus_directory"
    name_lower = path.name.lower()
    if any(pat in name_lower for pat in EXCLUDED_NAME_PATTERNS):
        return False, "metadata_or_readme"
    if any(pat in name_lower for pat in COMBINED_PATTERNS):
        return False, "combined_duplicate"
    return True, ""


def priority_and_category(path: Path):
    parts = path_parts_lower(path)
    root = source_root(path)
    rel = rel_path(path)

    if root == "crawler":
        if "txt" in parts:
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
    if len(Path(rel).parts) == 1:
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


def classify_quality(text: str):
    stripped = text.strip()
    norm = normalized_text(stripped)
    chars = len(norm)
    cn = chinese_count(stripped)
    cn_ratio = cn / max(chars, 1)
    replacement_ratio = stripped.count("\ufffd") / max(len(stripped), 1)
    nav_hits = sum(stripped.count(p) for p in NAV_PHRASES)
    nav_density = nav_hits / max(chars / 1000, 1)

    if chars < 500:
        return "不完整/过短", "文本量低于500字"
    if replacement_ratio > 0.015:
        return "不完整/乱码较多", "替换字符比例偏高"
    if nav_hits >= 6 and (chars < 2500 or nav_density >= 4):
        return "纯网站介绍/导航噪声", "导航与站点模板词密度较高"
    if chars < 1200:
        return "可用短文本", "文本较短，适合政策/新闻短篇语料"
    if chars < 3000:
        return "可用短文本", "文本低于3000字，需按篇章短文使用"
    if cn_ratio < 0.05 and chars < 5000:
        return "需复核", "中文比例低且文本不长"
    return "完整可用", "正文规模充足"


def load_exclusions(path: Path):
    if not path.exists():
        return set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {str(row.get("path", "")).strip().replace("\\", "/").casefold() for row in csv.DictReader(handle) if str(row.get("path", "")).strip()}


def collect_files(root: Path, exclusions=None):
    records = []
    skipped = Counter()
    exclusions = exclusions or set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        project_relative = str(path.resolve().relative_to(PROJECT_ROOT.resolve())).replace("\\", "/").casefold()
        if project_relative in exclusions:
            skipped["user_excluded"] += 1
            continue
        ok, reason = is_candidate(path)
        if not ok:
            skipped[reason] += 1
            continue
        try:
            text = read_text(path)
        except Exception as exc:
            skipped[f"read_error:{type(exc).__name__}"] += 1
            continue
        norm = normalized_text(text)
        if not norm:
            skipped["empty_after_normalize"] += 1
            continue
        priority, category, source = priority_and_category(path)
        quality, quality_reason = classify_quality(text)
        records.append(
            {
                "path": str(path),
                "relative_path": rel_path(path),
                "root": source_root(path),
                "name": path.name,
                "stem": path.stem,
                "suffix": path.suffix.lower(),
                "size_bytes": path.stat().st_size,
                "priority": priority,
                "category": category,
                "source": source,
                "family_key": family_key(path),
                "norm_hash": hashlib.sha256(norm.encode("utf-8")).hexdigest(),
                "norm_chars": len(norm),
                "raw_chars": len(text),
                "chinese_chars": chinese_count(text),
                "quality": quality,
                "quality_reason": quality_reason,
            }
        )
    return records, skipped


def deduplicate(records):
    selected = {}
    duplicate_of = {}

    def rank(rec):
        usable_rank = {
            "完整可用": 4,
            "可用短文本": 3,
            "需复核": 2,
            "纯网站介绍/导航噪声": 1,
            "不完整/乱码较多": 0,
            "不完整/过短": 0,
        }.get(rec["quality"], 0)
        return (usable_rank, rec["priority"], rec["norm_chars"], -len(rec["path"]))

    for rec in records:
        key = rec["family_key"] or f"hash::{rec['norm_hash']}"
        current = selected.get(key)
        if current is None or rank(rec) > rank(current):
            if current is not None:
                duplicate_of[current["path"]] = rec["path"]
            selected[key] = rec
        else:
            duplicate_of[rec["path"]] = current["path"]

    # A second exact-hash pass catches duplicated files across unrelated families.
    final = {}
    final_duplicates = dict(duplicate_of)
    for rec in selected.values():
        key = f"hash::{rec['norm_hash']}"
        current = final.get(key)
        if current is None or rank(rec) > rank(current):
            if current is not None:
                final_duplicates[current["path"]] = rec["path"]
            final[key] = rec
        else:
            final_duplicates[rec["path"]] = current["path"]

    kept_paths = {rec["path"] for rec in final.values()}
    for rec in records:
        rec["dedup_status"] = "kept" if rec["path"] in kept_paths else "duplicate"
        rec["duplicate_of"] = final_duplicates.get(rec["path"], "")
    return list(final.values()), records


def summarize(kept, all_records, skipped):
    usable = [r for r in kept if r["quality"] in {"完整可用", "可用短文本"}]
    complete = [r for r in kept if r["quality"] == "完整可用"]
    by_root = Counter(r["root"] for r in usable)
    by_category = Counter(r["category"] for r in usable)
    by_source = Counter(r["source"] for r in usable)
    by_quality = Counter(r["quality"] for r in kept)
    chars_by_category = defaultdict(int)
    for rec in usable:
        chars_by_category[rec["category"]] += rec["norm_chars"]

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_roots": [str(BOOK_ROOT), str(CRAWLER_ROOT)],
        "scanned_candidate_text_files": len(all_records),
        "kept_after_dedup": len(kept),
        "duplicates_removed": sum(1 for r in all_records if r["dedup_status"] == "duplicate"),
        "usable_after_quality_filter": len(usable),
        "complete_usable": len(complete),
        "usable_norm_chars": sum(r["norm_chars"] for r in usable),
        "complete_norm_chars": sum(r["norm_chars"] for r in complete),
        "by_root": dict(by_root),
        "by_category": dict(by_category),
        "by_source": dict(by_source),
        "by_quality_all_kept": dict(by_quality),
        "chars_by_category": dict(sorted(chars_by_category.items(), key=lambda x: x[1], reverse=True)),
        "skipped_files_by_reason": dict(skipped),
    }


def write_outputs(summary, all_records, kept):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    detail_path = OUT_DIR / "corpus_inventory_details.csv"
    summary_path = OUT_DIR / "corpus_inventory_summary.json"
    readme_path = OUT_DIR / "README_corpus_inventory.md"

    fields = [
        "dedup_status",
        "root",
        "category",
        "source",
        "quality",
        "quality_reason",
        "norm_chars",
        "raw_chars",
        "chinese_chars",
        "size_bytes",
        "priority",
        "relative_path",
        "path",
        "duplicate_of",
        "norm_hash",
    ]
    with detail_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for rec in sorted(all_records, key=lambda r: (r["root"], r["category"], r["source"], r["relative_path"])):
            writer.writerow({field: rec.get(field, "") for field in fields})

    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    usable_rows = [r for r in kept if r["quality"] in {"完整可用", "可用短文本"}]
    top_examples = sorted(usable_rows, key=lambda r: r["norm_chars"], reverse=True)[:20]
    lines = [
        "# Corpus Inventory",
        "",
        f"- 生成时间：{summary['generated_at']}",
        f"- 输入目录：`{BOOK_ROOT}`；`{CRAWLER_ROOT}`",
        f"- 候选文本文件：{summary['scanned_candidate_text_files']}",
        f"- 去重后保留：{summary['kept_after_dedup']}",
        f"- 去重剔除：{summary['duplicates_removed']}",
        f"- 质量过滤后可用：{summary['usable_after_quality_filter']}",
        f"- 其中完整可用：{summary['complete_usable']}",
        f"- 可用正文规范化字符数：{summary['usable_norm_chars']}",
        "",
        "## 按目录",
    ]
    for key, value in sorted(summary["by_root"].items()):
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("## 按内容类型")
    for key, value in sorted(summary["by_category"].items(), key=lambda x: x[1], reverse=True):
        chars = summary["chars_by_category"].get(key, 0)
        lines.append(f"- {key}: {value} 篇/本，约 {chars} 规范化字符")
    lines.append("")
    lines.append("## 按来源")
    for key, value in sorted(summary["by_source"].items(), key=lambda x: x[1], reverse=True):
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("## 最大的可用文本示例")
    for rec in top_examples:
        lines.append(f"- {rec['category']} | {rec['source']} | {rec['norm_chars']} 字 | `{rec['relative_path']}`")
    lines.append("")
    lines.append("## 判定规则摘要")
    lines.append("- 优先采用 `txt_reflowed`、简体化文本、爬虫正文抽取目录，排除 OCR 单页碎片、raw_html、日志、脚本、配置、元数据和 README。")
    lines.append("- 对同一教材的 PDF/EPUB 抽取版、OCR 版、重排行版按同名族去重，优先保留重排行版；再按规范化正文哈希做精确去重。")
    lines.append("- 少于 500 字、乱码较多、导航模板词密度高的文本不计入可用正文；短政策/新闻类正文单独标为可用短文本。")
    readme_path.write_text("\n".join(lines), encoding="utf-8")

    return detail_path, summary_path, readme_path


def main():
    args = parse_args()
    configure_paths(args.manual_root, args.web_root, args.output_dir)
    exclusions = load_exclusions(args.exclude_manifest)
    book_records, book_skipped = collect_files(BOOK_ROOT, exclusions)
    crawler_records, crawler_skipped = collect_files(CRAWLER_ROOT, exclusions)
    skipped = book_skipped + crawler_skipped
    kept, all_records = deduplicate(book_records + crawler_records)
    summary = summarize(kept, all_records, skipped)
    detail_path, summary_path, readme_path = write_outputs(summary, all_records, kept)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"DETAIL={detail_path}")
    print(f"SUMMARY={summary_path}")
    print(f"README={readme_path}")


if __name__ == "__main__":
    main()
