#!/usr/bin/env python3
"""Conservative crawler for configurable topic-corpus source discovery.

The crawler only collects public HTML pages and attachment links. It does not
log in, bypass anti-bot controls, or call private APIs.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
from html.parser import HTMLParser
import json
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.request import Request, urlopen


ATTACHMENT_EXTENSIONS = (
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".csv",
    ".zip",
    ".rar",
)


DATE_RE = re.compile(r"(20\d{2}[-年./]\d{1,2}[-月./]\d{1,2})")
SPACE_RE = re.compile(r"[ \t\r\f\v]+")
BLANK_RE = re.compile(r"\n{3,}")


class TextLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._tag_stack: list[str] = []
        self._current_href: str | None = None
        self._current_link_text: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        self._tag_stack.append(tag)
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        if tag == "a":
            attrs_dict = {k.lower(): v for k, v in attrs if k}
            self._current_href = attrs_dict.get("href")
            self._current_link_text = []
        if tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a" and self._current_href:
            text = normalize_text("".join(self._current_link_text))
            self.links.append((self._current_href, text))
            self._current_href = None
            self._current_link_text = []
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        if self._tag_stack:
            self._tag_stack.pop()
        if tag in {"p", "div", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = html.unescape(data)
        if self._tag_stack and self._tag_stack[-1] == "title":
            self.title_parts.append(text)
        self.text_parts.append(text)
        if self._current_href is not None:
            self._current_link_text.append(text)


@dataclass
class PageResult:
    site: str
    url: str
    title: str
    date: str
    text: str
    html_path: str
    txt_path: str


def normalize_text(value: str) -> str:
    value = html.unescape(value)
    value = SPACE_RE.sub(" ", value)
    lines = [line.strip() for line in value.splitlines()]
    value = "\n".join(line for line in lines if line)
    value = BLANK_RE.sub("\n\n", value)
    return value.strip()


def url_hash(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def same_allowed_domain(url: str, domains: Iterable[str]) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host == d.lower() or host.endswith("." + d.lower()) for d in domains)


def is_http_url(url: str) -> bool:
    return urlparse(url).scheme in {"http", "https"}


def is_attachment(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in ATTACHMENT_EXTENSIONS)


def contains_keyword(text: str, keywords: list[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def fetch(url: str, user_agent: str, timeout: int) -> tuple[bytes, str]:
    req = Request(url, headers={"User-Agent": user_agent})
    with urlopen(req, timeout=timeout) as resp:
        content_type = resp.headers.get("Content-Type", "")
        return resp.read(), content_type


def decode_html(raw: bytes, content_type: str) -> str:
    charset = "utf-8"
    match = re.search(r"charset=([\w.-]+)", content_type, flags=re.I)
    if match:
        charset = match.group(1)
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def extract_date(text: str) -> str:
    match = DATE_RE.search(text)
    if not match:
        return ""
    return (
        match.group(1)
        .replace("年", "-")
        .replace("月", "-")
        .replace("/", "-")
        .replace(".", "-")
        .replace("日", "")
    )


def write_csv_row(path: Path, fieldnames: list[str], row: dict[str, str]) -> None:
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def save_page(
    site: str,
    url: str,
    raw_html: str,
    parser: TextLinkParser,
    output_dir: Path,
) -> PageResult:
    title = normalize_text("".join(parser.title_parts))
    text = normalize_text("".join(parser.text_parts))
    date = extract_date(text)
    item_id = url_hash(url)

    html_dir = output_dir / "raw_html" / site
    txt_dir = output_dir / "txt" / site
    html_dir.mkdir(parents=True, exist_ok=True)
    txt_dir.mkdir(parents=True, exist_ok=True)

    html_path = html_dir / f"{item_id}.html"
    txt_path = txt_dir / f"{item_id}.txt"
    html_path.write_text(raw_html, encoding="utf-8")
    txt_path.write_text(
        f"URL: {url}\nTITLE: {title}\nDATE: {date}\n\n{text}\n",
        encoding="utf-8",
    )
    return PageResult(site, url, title, date, text, str(html_path), str(txt_path))


def crawl_site(site_cfg: dict, cfg: dict, output_dir: Path) -> None:
    site_name = site_cfg["name"]
    allowed_domains = site_cfg["allowed_domains"]
    keywords = cfg["keywords"]
    user_agent = cfg["user_agent"]
    timeout = int(cfg.get("timeout_seconds", 20))
    delay = float(cfg.get("delay_seconds", 1.0))
    max_pages = int(cfg.get("max_pages_per_site", 30))
    max_depth = int(cfg.get("max_depth", 2))

    queue: deque[tuple[str, int]] = deque((seed, 0) for seed in site_cfg["seeds"])
    seen: set[str] = set()
    saved_count = 0

    article_csv = output_dir / "metadata" / "articles.csv"
    attach_csv = output_dir / "metadata" / "attachments.csv"
    log_csv = output_dir / "logs" / "crawl_log.csv"
    for p in [article_csv.parent, attach_csv.parent, log_csv.parent]:
        p.mkdir(parents=True, exist_ok=True)

    while queue and saved_count < max_pages:
        url, depth = queue.popleft()
        url, _ = urldefrag(url)
        if url in seen or not is_http_url(url):
            continue
        if not same_allowed_domain(url, allowed_domains):
            continue
        seen.add(url)

        try:
            raw, content_type = fetch(url, user_agent, timeout)
            status = "ok"
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            write_csv_row(
                log_csv,
                ["site", "url", "status", "message"],
                {"site": site_name, "url": url, "status": "error", "message": str(exc)},
            )
            time.sleep(delay)
            continue

        if "text/html" not in content_type and "application/xhtml" not in content_type:
            time.sleep(delay)
            continue

        raw_html = decode_html(raw, content_type)
        parser = TextLinkParser()
        parser.feed(raw_html)
        page_text = normalize_text("".join(parser.title_parts) + "\n" + "".join(parser.text_parts))

        matching_page = contains_keyword(page_text, keywords)
        if matching_page:
            result = save_page(site_name, url, raw_html, parser, output_dir)
            saved_count += 1
            write_csv_row(
                article_csv,
                ["site", "url", "title", "date", "html_path", "txt_path"],
                {
                    "site": result.site,
                    "url": result.url,
                    "title": result.title,
                    "date": result.date,
                    "html_path": result.html_path,
                    "txt_path": result.txt_path,
                },
            )

        for href, link_text in parser.links:
            absolute = urljoin(url, href)
            absolute, _ = urldefrag(absolute)
            if not is_http_url(absolute) or not same_allowed_domain(absolute, allowed_domains):
                continue
            link_blob = f"{link_text}\n{absolute}"
            if is_attachment(absolute) and contains_keyword(page_text + "\n" + link_blob, keywords):
                write_csv_row(
                    attach_csv,
                    ["site", "page_url", "attachment_url", "link_text"],
                    {
                        "site": site_name,
                        "page_url": url,
                        "attachment_url": absolute,
                        "link_text": link_text,
                    },
                )
            elif depth < max_depth:
                if contains_keyword(link_blob, keywords) or depth == 0:
                    queue.append((absolute, depth + 1))

        write_csv_row(
            log_csv,
            ["site", "url", "status", "message"],
            {"site": site_name, "url": url, "status": status, "message": f"saved={matching_page}"},
        )
        time.sleep(delay)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="Path to crawler JSON config.")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    output_dir = Path(cfg["output_dir"])
    if not output_dir.is_absolute():
        output_dir = (cfg_path.resolve().parent / output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    for site_cfg in cfg["sites"]:
        crawl_site(site_cfg, cfg, output_dir)


if __name__ == "__main__":
    main()
