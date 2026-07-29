from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path


DEFAULT_INPUT = Path(r"D:\production\corpus\book\new_energy_vehicle_book\txt")
DEFAULT_OUTPUT = Path(r"D:\production\corpus\book\new_energy_vehicle_book\txt_reflowed")


SENTENCE_END = set("。！？!?；;")
SOFT_END = set("，,、：:")
OPENING = set("（([【《“‘")
CLOSING = set("）)]】》”’")


@dataclass
class FileStats:
    source: str
    output: str
    input_lines: int
    output_lines: int
    input_chars: int
    output_chars: int
    page_markers_removed: int
    paragraphs_joined: int


def normalize_line(line: str) -> str:
    line = line.replace("\ufeff", "")
    line = re.sub(r"[ \t\u3000]+", " ", line)
    return line.strip()


def is_page_marker(line: str) -> bool:
    return bool(re.fullmatch(r"=+\s*第\s*\d+\s*页\s*=+", line.strip()))


def is_metadata_line(line: str, line_no: int) -> bool:
    if line_no > 8:
        return False
    return line.startswith(("来源文件：", "转换时间：", "识别方式：", "说明："))


def is_probable_page_noise(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if re.fullmatch(r"[IVXLCDMⅰ-ⅻⅠ-Ⅻ]+", s):
        return True
    if re.fullmatch(r"[-—–]?\s*\d{1,4}\s*[-—–]?", s):
        return True
    return False


def is_heading(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if len(s) > 42:
        return False
    if re.fullmatch(r"(第[一二三四五六七八九十百\d]+[章节篇部分].*)", s):
        return True
    if re.fullmatch(r"(模块|项目|任务|学习任务)[一二三四五六七八九十\d]+.*", s):
        return True
    if re.fullmatch(r"[一二三四五六七八九十]+[、.．]\s*[^。！？!?；;]{1,36}", s):
        return True
    if re.fullmatch(r"\d+(\.\d+){0,4}\s+[^。！？!?；;]{1,42}", s):
        return True
    if s in {"前言", "目录", "参考文献", "附录", "编者", "序", "后记"}:
        return True
    return False


def is_list_or_table_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if re.match(r"^([（(]?\d+[）).、]|[A-Z]\.|[①②③④⑤⑥⑦⑧⑨⑩]|[·•●])\s*", s):
        return True
    if re.match(r"^(表|图)\s*\d", s):
        return True
    # Lines with many separated numeric/Latin fields are usually tables.
    tokens = s.split()
    if len(s) > 28 and re.search(r"[\u4e00-\u9fff]", s) and any(p in s for p in "，,。；;"):
        return False
    if len(tokens) >= 4:
        numeric_like = sum(bool(re.search(r"[\dA-Za-z/%Ω℃.-]", t)) for t in tokens)
        if numeric_like >= 4 and numeric_like / len(tokens) >= 0.6:
            return True
    # Connector pin definitions often depend on line layout.
    if any(k in s for k in ("针脚", "端子", "引脚", "序号", "编号", "定义")) and len(tokens) >= 3:
        return True
    return False


def ends_sentence(line: str) -> bool:
    s = line.rstrip()
    while s and s[-1] in CLOSING:
        s = s[:-1]
    return bool(s and s[-1] in SENTENCE_END)


def should_join(prev: str, cur: str) -> bool:
    if not prev or not cur:
        return False
    if is_heading(prev) or is_heading(cur):
        return False
    if is_list_or_table_line(prev) or is_list_or_table_line(cur):
        return False
    if is_probable_page_noise(prev) or is_probable_page_noise(cur):
        return False
    if cur[0] in CLOSING:
        return True
    if prev[-1] in OPENING:
        return True
    if prev[-1] in SOFT_END:
        return True
    if ends_sentence(prev):
        return False
    # OCR/PDF hard wraps in Chinese textbooks usually split lines at 25-45 chars.
    if re.search(r"[\u4e00-\u9fff]$", prev) and re.match(r"^[\u4e00-\u9fffA-Za-z0-9]", cur):
        return True
    # English/number fragments inside technical prose.
    if re.search(r"[A-Za-z0-9%-]$", prev) and re.match(r"^[A-Za-z0-9%()（）]", cur):
        return True
    return False


def join_lines(prev: str, cur: str) -> str:
    if not prev:
        return cur
    if not cur:
        return prev
    if prev[-1] in OPENING or cur[0] in CLOSING:
        return prev + cur
    # Avoid inserting spaces in normal Chinese prose; keep a space for Latin word boundaries.
    if re.search(r"[A-Za-z0-9]$", prev) and re.match(r"^[A-Za-z0-9]", cur):
        return prev + " " + cur
    return prev + cur


def split_overlong_paragraph(text: str, max_len: int = 260) -> list[str]:
    if len(text) <= max_len:
        return [text]
    pieces: list[str] = []
    buf = ""
    for part in re.split(r"(?<=[。！？!?；;])", text):
        if not part:
            continue
        if len(buf) + len(part) > max_len and buf:
            pieces.append(buf)
            buf = part
        else:
            buf += part
    if buf:
        pieces.append(buf)
    return pieces or [text]


def reflow_text(raw: str) -> tuple[str, dict[str, int]]:
    raw_lines = raw.splitlines()
    logical: list[str] = []
    page_markers_removed = 0
    paragraphs_joined = 0

    for idx, original in enumerate(raw_lines, 1):
        line = normalize_line(original)
        if is_page_marker(line):
            page_markers_removed += 1
            continue
        if not line:
            next_line = ""
            for probe in raw_lines[idx:]:
                candidate = normalize_line(probe)
                if is_page_marker(candidate):
                    continue
                if candidate:
                    next_line = candidate
                    break
            if logical and logical[-1] and next_line and should_join(logical[-1], next_line):
                continue
            if logical and logical[-1] != "":
                logical.append("")
            continue
        if is_metadata_line(line, idx):
            logical.append(line)
            continue
        if is_probable_page_noise(line):
            continue

        if logical and logical[-1] and should_join(logical[-1], line):
            logical[-1] = join_lines(logical[-1], line)
            paragraphs_joined += 1
        else:
            logical.append(line)

    # Collapse repeated blank lines and split very long prose paragraphs on sentence boundaries.
    output: list[str] = []
    for line in logical:
        if not line:
            if output and output[-1] != "":
                output.append("")
            continue
        if is_list_or_table_line(line) or is_heading(line) or is_metadata_line(line, 1):
            output.append(line)
        else:
            output.extend(split_overlong_paragraph(line))

    while output and output[-1] == "":
        output.pop()
    return "\n".join(output) + "\n", {
        "page_markers_removed": page_markers_removed,
        "paragraphs_joined": paragraphs_joined,
    }


def process_file(src: Path, out_dir: Path) -> FileStats:
    text = src.read_text(encoding="utf-8", errors="replace")
    repaired, counters = reflow_text(text)
    out_path = out_dir / src.name
    out_path.write_text(repaired, encoding="utf-8")
    return FileStats(
        source=str(src),
        output=str(out_path),
        input_lines=len(text.splitlines()),
        output_lines=len(repaired.splitlines()),
        input_chars=len(text),
        output_chars=len(repaired),
        page_markers_removed=counters["page_markers_removed"],
        paragraphs_joined=counters["paragraphs_joined"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair hard OCR/PDF line breaks in Chinese textbook TXT files.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Input directory containing .txt files.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output directory for repaired .txt files.")
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    stats: list[FileStats] = []
    for src in sorted(args.input.glob("*.txt")):
        if src.name.endswith(".bak.txt"):
            continue
        item = process_file(src, args.output)
        stats.append(item)
        print(f"reflowed {src.name}: {item.input_lines} -> {item.output_lines} lines")

    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_dir": str(args.input),
        "output_dir": str(args.output),
        "method": "Heuristic Chinese OCR/PDF hard-line reflow; preserves headings, lists, table-like rows, and metadata.",
        "files": [asdict(s) for s in stats],
    }
    (args.output / "reflow_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "README_reflow.md").write_text(
        "\n".join(
            [
                "# TXT Reflow Output",
                "",
                f"Generated at: {manifest['generated_at']}",
                f"Input: {args.input}",
                f"Output: {args.output}",
                "",
                "This directory contains repaired TXT files generated from OCR/PDF-extracted textbooks.",
                "The script joins hard-wrapped Chinese prose lines, removes page markers, and preserves likely headings, lists, formulas, and table-like rows.",
                "",
                "Script: scripts/repair_ocr_linebreaks.py",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
