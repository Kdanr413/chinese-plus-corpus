"""Evaluated grammar- and evidence-driven term recognition core.

This module is integrated from the separately verified
``术语识别流程-识别效果核验`` workflow.  It intentionally produces candidates
and evidence only; approval remains the responsibility of the platform's
validation and decision stages.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

import jieba
import jieba.posseg as pseg


HAN_RUN = re.compile(r"[\u3400-\u9fff]{2,}")
TECHNICAL_EXPRESSION = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9.+/_-]{1,15}(?![A-Za-z0-9])")
SENTENCE_SPAN = re.compile(r"[^\n。！？!?；;]+(?:[。！？!?；;]+|$)")


@dataclass(frozen=True)
class Token:
    text: str
    pos: str
    start: int
    end: int


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_recognition_config(path: Path) -> dict[str, Any]:
    """Load recognition settings and the separately editable grammar module."""
    config = read_json(path)
    grammar_settings = config.get("candidate_grammar", {})
    profile_file = str(grammar_settings.get("profile_file") or "").strip()
    if profile_file:
        profile_path = Path(profile_file)
        if not profile_path.is_absolute():
            profile_path = path.parent / profile_path
        if not profile_path.is_file():
            raise FileNotFoundError(f"术语语法模板文件不存在：{profile_path}")
        grammar_module = read_json(profile_path)
        profiles = grammar_module.get("profiles", {})
        default_profile = str(grammar_settings.get("default_profile") or "")
        override = str(grammar_settings.get("profile_override") or "").strip()
        for profile_name in [default_profile, override, *grammar_settings.get("domain_profile_map", {}).values()]:
            if profile_name and profile_name not in profiles:
                raise ValueError(f"未知术语语法模板：{profile_name}")
        config["candidate_grammar"] = {
            **grammar_settings,
            "profile_path": str(profile_path.resolve()),
            "pos_classes": grammar_module.get("pos_classes", {}),
            "profiles": profiles,
        }
    return config


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def read_trusted_terms(path: Path) -> dict[str, dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = csv.DictReader(handle)
        result: dict[str, dict[str, str]] = {}
        for row in rows:
            term = str(row.get("term", "")).strip()
            if term and str(row.get("status", "approved")).strip().lower() in {"approved", "active", "通过"}:
                result[term] = {str(key): str(value or "") for key, value in row.items()}
        return result


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _event_query_id(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("query_id", "id"):
            if value.get(key) is not None:
                return str(value[key])
        for child in value.values():
            found = _event_query_id(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _event_query_id(child)
            if found:
                return found
    return ""


def _event_count(value: Any) -> int | None:
    """Accept common streaming-event layouts without confusing booleans for counts."""
    if isinstance(value, dict):
        for key in ("count", "total_hits", "hit_count", "total", "frequency"):
            current = value.get(key)
            if isinstance(current, (int, float)) and not isinstance(current, bool):
                return max(0, int(current))
        result = value.get("result")
        if isinstance(result, (int, float)) and not isinstance(result, bool):
            return max(0, int(result))
        for child in value.values():
            found = _event_count(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _event_count(child)
            if found is not None:
                return found
    return None


def query_bcc_counts(
    terms: list[str], *, endpoint: str, channel: str, api_key: str,
    batch_size: int, timeout_seconds: int,
    progress_callback: Callable[[int, int, str], None] | None = None,
    batch_result_callback: Callable[[dict[str, int]], None] | None = None,
) -> tuple[dict[str, int], list[str]]:
    """Query BCC in batches; batch size is not an API-access ceiling."""
    try:
        import requests  # type: ignore
    except ImportError:
        requests = None

    counts: dict[str, int] = {}
    errors: list[str] = []
    for offset in range(0, len(terms), batch_size):
        batch = terms[offset : offset + batch_size]
        batch_counts: dict[str, int] = {}
        id_to_term = {f"q{offset + index + 1}": term for index, term in enumerate(batch)}
        payload = {
            "channel": channel,
            "queries": [
                {"query_id": query_id, "expression": term, "operation": "count"}
                for query_id, term in id_to_term.items()
            ],
        }
        try:
            url = endpoint.rstrip("/") + "/api/v1/batches"
            if requests is not None:
                response_context = requests.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload, stream=True, timeout=timeout_seconds,
                )
            else:
                # Keep the formal workflow runnable in a clean Python install;
                # BCC access does not require the third-party requests package.
                from urllib.request import Request, urlopen

                request = Request(
                    url,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    method="POST",
                )
                response_context = urlopen(request, timeout=timeout_seconds)

            with response_context as response:
                if requests is not None:
                    response.raise_for_status()
                    raw_lines = response.iter_lines()
                else:
                    raw_lines = response
                for raw in raw_lines:
                    if not raw:
                        continue
                    event = json.loads(raw)
                    query_id = _event_query_id(event)
                    count = _event_count(event)
                    if query_id in id_to_term and count is not None:
                        term = id_to_term[query_id]
                        counts[term] = count
                        batch_counts[term] = count
                    event_type = str(event.get("type") or event.get("event") or "").lower() if isinstance(event, dict) else ""
                    if "error" in event_type:
                        detail = ""
                        if isinstance(event, dict):
                            detail = str(event.get("message") or event.get("error") or event.get("detail") or "")
                        errors.append(f"{query_id or 'batch'}: server error event{': ' + detail if detail else ''}")
        except Exception as exc:
            errors.append(f"batch starting at {offset}: {type(exc).__name__}: {exc}")
        missing_in_batch = [term for term in batch if term not in batch_counts]
        if missing_in_batch:
            errors.append(
                f"batch starting at {offset}: no count returned for {len(missing_in_batch)}/{len(batch)} queries"
            )
        if batch_counts and batch_result_callback is not None:
            batch_result_callback(batch_counts)
        if progress_callback is not None:
            progress_callback(
                min(offset + len(batch), len(terms)), len(terms),
                f"BCC批次 {offset // batch_size + 1}/{math.ceil(len(terms) / batch_size)}",
            )
    return counts, errors


def load_bcc_cache(path: Path, channel: str) -> dict[str, int]:
    if not path.is_file():
        return {}
    try:
        payload = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if payload.get("channel") != channel:
        return {}
    return {
        str(term): int(row["count"] if isinstance(row, dict) else row)
        for term, row in payload.get("counts", {}).items()
        if isinstance(row, (dict, int, float))
    }


def save_bcc_cache(path: Path, *, endpoint: str, channel: str, counts: dict[str, int]) -> None:
    now = datetime.now().astimezone().isoformat()
    write_json(path, {
        "schema_version": "1.0", "endpoint": endpoint, "channel": channel,
        "updated_at": now,
        "counts": {term: {"count": count, "queried_at": now} for term, count in sorted(counts.items())},
        "note": "No API credential is stored in this cache.",
    })

def is_all_upper_english_term(term: str) -> bool:
    """
    判断英文术语中的字母是否全部大写。

    保留：
    AI、GPT-4、CO2、5G、COVID-19、B2B

    删除：
    Angrist、Econometrica、Covid、eDNA、Using
    """
    letters = re.findall(r"[A-Za-z]", term)
    return bool(letters) and all(letter.isupper() for letter in letters)

def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value.replace("\x00", "")).strip()


def load_local_env(path: Path) -> None:
    """Load missing values from a local untracked .env without logging secrets."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and value and name not in os.environ:
            os.environ[name] = value


def expand_to_analysis_units(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Split complete documents into traceable sentences; keep sentence input unchanged."""
    units: list[dict[str, Any]] = []
    document_ids: set[str] = set()
    for row_number, row in enumerate(rows, start=1):
        document_id = str(row.get("doc_id") or row.get("document_id") or f"document_{row_number:04d}")
        document_ids.add(document_id)
        text = normalize_text(str(row.get("text") or row.get("clean_text") or ""))
        if not text:
            continue
        if row.get("sentence_id"):
            units.append({**row, "doc_id": document_id, "text": text, "document_start_offset": 0})
            continue
        sentence_index = 0
        for match in SENTENCE_SPAN.finditer(text):
            raw = match.group(0)
            leading = len(raw) - len(raw.lstrip())
            sentence = raw.strip()
            if not sentence:
                continue
            sentence_index += 1
            units.append({
                "doc_id": document_id,
                "sentence_id": f"{document_id}__sentence_{sentence_index:06d}",
                "text": sentence,
                "document_start_offset": match.start() + leading,
                "source": row.get("source", ""),
                "relative_path": row.get("relative_path", ""),
                "title": row.get("title", ""),
                "domain": row.get("domain", ""),
                "source_type": row.get("source_type", ""),
                "text_genre": row.get("text_genre", ""),
            })
    return units, len(document_ids)


def tokenise(
    text: str,
    tokenizer: jieba.Tokenizer,
    tagger: pseg.POSTokenizer | None = None,
) -> list[Token]:
    # POSTokenizer consumes the Tokenizer's user_word_tag_tab when it is
    # constructed. Reconstructing it for every sentence therefore preserves
    # custom tags only in the first sentence and silently changes later terms
    # to ``x``. Callers processing a corpus must reuse one tagger instance.
    tagger = tagger or pseg.POSTokenizer(tokenizer)
    cursor = 0
    tokens: list[Token] = []
    for pair in tagger.cut(text, HMM=True):
        word = pair.word
        start = text.find(word, cursor)
        if start < 0:
            start = cursor
        end = start + len(word)
        tokens.append(Token(word, pair.flag, start, end))
        cursor = end
    return tokens


def is_noun(pos: str, noun_prefixes: tuple[str, ...]) -> bool:
    return pos.startswith(noun_prefixes)


def _template_parts(template: str) -> list[tuple[str, int, int | None]]:
    parts: list[tuple[str, int, int | None]] = []
    for raw in template.split():
        quantifier = raw[-1] if raw and raw[-1] in "?*+" else ""
        name = raw[:-1] if quantifier else raw
        minimum, maximum = {
            "": (1, 1), "?": (0, 1), "*": (0, None), "+": (1, None),
        }[quantifier]
        if not name:
            raise ValueError(f"无效语法模板：{template}")
        parts.append((name, minimum, maximum))
    return parts


def _pos_matches_class(pos: str, class_name: str, pos_classes: dict[str, list[str]]) -> bool:
    if class_name not in pos_classes:
        raise ValueError(f"语法模板引用了未知词性类别：{class_name}")
    return pos.startswith(tuple(map(str, pos_classes[class_name])))


def pos_template_matches(tokens: list[Token], template: str, pos_classes: dict[str, list[str]]) -> bool:
    """Match a complete POS sequence against the small configurable template DSL."""
    parts = _template_parts(template)
    memo: dict[tuple[int, int], bool] = {}

    def match(part_index: int, token_index: int) -> bool:
        key = (part_index, token_index)
        if key in memo:
            return memo[key]
        if part_index == len(parts):
            return token_index == len(tokens)
        class_name, minimum, maximum = parts[part_index]
        available = 0
        limit = len(tokens) - token_index if maximum is None else min(maximum, len(tokens) - token_index)
        while available < limit and _pos_matches_class(tokens[token_index + available].pos, class_name, pos_classes):
            available += 1
        memo[key] = any(match(part_index + 1, token_index + count) for count in range(minimum, available + 1))
        return memo[key]

    return match(0, 0)


def grammar_profile_for_row(row: dict[str, Any], config: dict[str, Any]) -> str:
    grammar = config.get("candidate_grammar", {})
    override = str(grammar.get("profile_override") or "").strip()
    if override:
        return override
    domain_field = str(grammar.get("domain_field") or "domain")
    domain = str(row.get(domain_field) or "").strip()
    mapping = grammar.get("domain_profile_map", {})
    if domain in mapping:
        return str(mapping[domain])
    for marker, profile in mapping.items():
        if str(marker) and str(marker) in domain:
            return str(profile)
    return str(grammar.get("default_profile") or "general_nominal")


def pos_pattern_valid(tokens: list[Token], config: dict[str, Any], profile_name: str | None = None) -> bool:
    if not tokens:
        return False
    grammar = config.get("candidate_grammar", {})
    profiles = grammar.get("profiles", {})
    if profiles:
        selected = profile_name or str(grammar.get("profile_override") or grammar.get("default_profile") or "general_nominal")
        if selected not in profiles:
            raise ValueError(f"未知术语语法模板：{selected}")
        profile = profiles[selected]
        pos_classes = grammar.get("pos_classes", {})
        excluded = any(pos_template_matches(tokens, template, pos_classes) for template in profile.get("exclude_templates", []))
        included = any(pos_template_matches(tokens, template, pos_classes) for template in profile.get("include_templates", []))
        return included and not excluded
    # Backward compatibility for historical frozen configurations.
    noun_prefixes = tuple(config["noun_pos_prefixes"])
    modifier_prefixes = tuple(config["modifier_pos_prefixes"])
    allowed = noun_prefixes + modifier_prefixes
    if any(not token.pos.startswith(allowed) for token in tokens):
        return False
    # Jieba uses ``vn`` for many verb/noun ambiguous words. A standalone vn
    # such as “发电” may be a term, and n+vn such as “碳捕集” may be a compound;
    # vn+n such as “发展新能源” is an action-object phrase, not a term name.
    if len(tokens) > 1 and tokens[0].pos.startswith(("v", "vn")):
        return False
    if not is_noun(tokens[-1].pos, noun_prefixes):
        return False
    return any(is_noun(token.pos, noun_prefixes) for token in tokens)


def candidate_id(term: str) -> str:
    return "TERM_CAND_" + hashlib.sha256(term.encode("utf-8")).hexdigest()[:12]


def add_occurrence(
    target: dict[tuple[str, int, int], dict[str, Any]], *, term: str, start: int, end: int,
    source: str, pos_pattern: str, row: dict[str, Any], trusted: bool = False,
    grammar_profile: str = "",
) -> None:
    # 统一过滤所有来源中包含小写英文字母的术语候选
    if re.search(r"[a-z]", term):
        return

    key = (term, start, end)
    current = target.get(key)
    sources = set(current.get("sources", [])) if current else set()
    sources.add(source)
    document_start = int(row.get("document_start_offset") or 0)
    target[key] = {
        "term": term,
        "start_offset": start,
        "end_offset": end,
        "sources": sorted(sources),
        "pos_pattern": pos_pattern or (current or {}).get("pos_pattern", ""),
        "grammar_profile": grammar_profile or (current or {}).get("grammar_profile", ""),
        "trusted": trusted or bool((current or {}).get("trusted")),
        "document_id": str(row.get("doc_id") or row.get("document_id") or ""),
        "sentence_id": str(row.get("sentence_id") or row.get("id") or ""),
        "sentence": normalize_text(str(row.get("text") or row.get("clean_text") or "")),
        "document_start_offset": document_start + start,
        "document_end_offset": document_start + end,
        "source": str(row.get("source") or ""),
        "relative_path": str(row.get("relative_path") or ""),
    }


def extract_occurrences(
    rows: list[dict[str, Any]], config: dict[str, Any], trusted_terms: dict[str, dict[str, str]],
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    tokenizer = jieba.Tokenizer()
    # Jieba occasionally splits a lexicalised technical process into an
    # ordinary function word plus a noun (for example 并/c + 网/n).  Merely
    # allowing more POS tags would then admit arbitrary conjunction phrases.
    # Keep these corrections explicit and configurable, and apply them before
    # the trusted terminology lexicon so candidate generation remains
    # independent from the evaluation reference.
    for item in config.get("candidate_grammar", {}).get("tokenizer_lexicon", []):
        if isinstance(item, str):
            term, pos = item.strip(), "nz"
        else:
            term = str(item.get("term") or "").strip()
            pos = str(item.get("pos") or "nz").strip()
        if term:
            tokenizer.add_word(term, freq=20_000_000, tag=pos)
    for term in sorted(trusted_terms, key=len, reverse=True):
        tokenizer.add_word(term, freq=20_000_000, tag="nz")
    tagger = pseg.POSTokenizer(tokenizer)

    generic = set(config.get("generic_terms", []))
    generic_terminal_words = set(config.get("generic_terminal_words", []))
    min_chars = int(config.get("min_characters", 2))
    max_chars = int(config.get("max_characters", 14))
    max_tokens = int(config.get("max_phrase_tokens", 4))
    all_occurrences: list[dict[str, Any]] = []
    token_rows: list[dict[str, Any]] = []
    boundary_audit: list[dict[str, Any]] = []
    audit_focus = set(config.get("boundary_audit_focus_fragments", []))

    for row_number, row in enumerate(rows, start=1):
        text = normalize_text(str(row.get("text") or row.get("clean_text") or ""))
        if not text:
            continue
        if not row.get("sentence_id"):
            row = {**row, "sentence_id": f"sentence_{row_number:04d}"}
        tokens = tokenise(text, tokenizer, tagger)
        grammar_profile = grammar_profile_for_row(row, config)
        token_rows.append({
            "sentence_id": row["sentence_id"],
            "text": text,
            "tokens": [token.__dict__ for token in tokens],
            "grammar_profile": grammar_profile,
        })
        sentence_occurrences: dict[tuple[str, int, int], dict[str, Any]] = {}

        # Trusted terms are exact-match evidence, never fuzzy substring additions.
        for term in trusted_terms:
            start = 0
            while True:
                offset = text.find(term, start)
                if offset < 0:
                    break
                add_occurrence(
                    sentence_occurrences, term=term, start=offset, end=offset + len(term),
                    source="trusted_lexicon", pos_pattern="trusted", row=row, trusted=True,
                    grammar_profile=grammar_profile,
                )
                start = offset + len(term)

        content_tokens = [token for token in tokens if token.pos != "x" and token.text.strip()]
        for start_index, first in enumerate(content_tokens):
            for end_index in range(start_index, min(len(content_tokens), start_index + max_tokens)):
                span = content_tokens[start_index : end_index + 1]
                if any(left.end != right.start for left, right in zip(span, span[1:])):
                    break
                if not pos_pattern_valid(span, config, grammar_profile):
                    # A prefix need not itself be a complete term. For example,
                    # 虚拟/v is invalid alone but 虚拟/v+电厂/n matches VERB NOUN+;
                    # 绿色/a is invalid alone but 绿色/a+金融/n matches
                    # MODIFIER* NOUN+. Keep extending until max_tokens or a real
                    # token-boundary gap is reached.
                    continue
                term = "".join(token.text for token in span)
                if not (min_chars <= len(term) <= max_chars) or term in generic:
                    continue
                if len(span) > 1 and span[-1].text in generic_terminal_words:
                    continue
                add_occurrence(
                    sentence_occurrences, term=term, start=span[0].start, end=span[-1].end,
                    source="pos_grammar_profile", pos_pattern="+".join(token.pos for token in span),
                    row=row, trusted=term in trusted_terms,
                    grammar_profile=grammar_profile,
                )

        for match in TECHNICAL_EXPRESSION.finditer(text):
            english_term = match.group(0)

            # 只保留字母全部大写的英文术语
            if not is_all_upper_english_term(english_term):
                continue

            add_occurrence(
                sentence_occurrences,
                term=english_term,
                start=match.start(),
                end=match.end(),
                source="technical_expression",
                pos_pattern="eng",
                row=row,
                trusted=english_term in trusted_terms,
                grammar_profile=grammar_profile,
            )

        all_occurrences.extend(sentence_occurrences.values())

        token_starts = {token.start for token in tokens if token.pos != "x"}
        token_ends = {token.end for token in tokens if token.pos != "x"}
        accepted_spans = {(value["term"], value["start_offset"], value["end_offset"]) for value in sentence_occurrences.values()}
        for run in HAN_RUN.finditer(text):
            for size in range(2, min(6, len(run.group(0))) + 1):
                for local in range(len(run.group(0)) - size + 1):
                    start = run.start() + local
                    end = start + size
                    fragment = text[start:end]
                    if (fragment, start, end) in accepted_spans:
                        decision, reason = "accepted", "完整词边界且符合名词性候选规则"
                    elif start not in token_starts or end not in token_ends:
                        decision, reason = "rejected", "not_aligned_to_token_boundaries"
                    else:
                        covered = [token for token in tokens if start <= token.start and token.end <= end and token.pos != "x"]
                        if pos_pattern_valid(covered, config, grammar_profile):
                            decision, reason = "rejected", "valid_boundary_but_not_selected"
                        else:
                            pos_value = "+".join(token.pos for token in covered)
                            reason = "adverb_or_verb_phrase" if any(token.pos.startswith(("d", "v")) for token in covered) else "invalid_pos_pattern"
                            decision = "rejected"
                    # Full documents contain hundreds of thousands of arbitrary
                    # non-aligned n-grams. Keep all aligned decisions plus named
                    # regression fragments; count-free noise is intentionally omitted.
                    if reason == "not_aligned_to_token_boundaries" and fragment not in audit_focus:
                        continue
                    boundary_audit.append({
                        "sentence_id": row["sentence_id"], "fragment": fragment,
                        "start_offset": start, "end_offset": end,
                        "decision": decision, "reason": reason,
                        "sentence": text,
                    })

        if progress_callback is not None:
            progress_callback(row_number, len(rows))

    return all_occurrences, token_rows, boundary_audit


def aggregate_candidates(
    occurrences: list[dict[str, Any]], config: dict[str, Any], trusted_terms: dict[str, dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for occurrence in occurrences:
        grouped[occurrence["term"]].append(occurrence)
    minimum_frequency = int(config.get("minimum_corpus_frequency", 2))
    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for term, items in grouped.items():
        trusted = term in trusted_terms or any(item["trusted"] for item in items)
        frequency = len(items)
        documents = {item["document_id"] for item in items if item["document_id"]}
        sentences = {item["sentence_id"] for item in items}
        sources = sorted({source for item in items for source in item["sources"]})
        evidence_ok = trusted or frequency >= minimum_frequency or "technical_expression" in sources
        score = min(1.0, (0.45 if trusted else 0.0) + 0.25 + 0.12 * math.log2(frequency + 1) + 0.08 * min(len(documents), 2))
        base = {
            "candidate_id": candidate_id(term), "term": term,
            "occurrence_count": frequency, "document_count": len(documents),
            "sentence_count": len(sentences), "sources": "|".join(sources),
            "pos_patterns": "|".join(sorted({item["pos_pattern"] for item in items if item["pos_pattern"]})),
            "grammar_profiles": "|".join(sorted({item.get("grammar_profile", "") for item in items if item.get("grammar_profile")})),
            "trusted": "yes" if trusted else "no", "score": round(score, 4),
            "representative_contexts": " || ".join(dict.fromkeys(item["sentence"] for item in items))[:1200],
            "evidence_offsets": json.dumps([
                {
                    "sentence_id": item["sentence_id"],
                    "sentence_start": item["start_offset"], "sentence_end": item["end_offset"],
                    "document_start": item["document_start_offset"], "document_end": item["document_end_offset"],
                }
                for item in items[:10]
            ], ensure_ascii=False),
        }
        if evidence_ok:
            candidates.append({**base, "status": "candidate"})
        else:
            rejected.append({**base, "status": "rejected", "rejection_reason": "insufficient_corpus_evidence"})
    candidates.sort(key=lambda row: (row["trusted"] == "yes", row["score"], row["occurrence_count"], len(row["term"])), reverse=True)
    rejected.sort(key=lambda row: (row["score"], row["occurrence_count"]), reverse=True)
    return candidates, rejected


def apply_nested_candidate_gate(
    candidates: list[dict[str, Any]], rejected: list[dict[str, Any]],
    occurrences: list[dict[str, Any]], config: dict[str, Any] | None = None,
    analysis_units: list[dict[str, Any]] | None = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Distinguish structural nesting from genuine context-independent short-term use."""
    config = config or {}
    nested_config = config.get("nested_short_term", {})
    minimum_independent = int(nested_config.get("minimum_independent_occurrences", 2))
    stability_threshold = float(nested_config.get("boundary_stability_threshold", 0.8))
    long_context_threshold = float(nested_config.get("long_form_context_ratio_threshold", 0.8))
    context_window = int(nested_config.get("context_sentence_window", 1))
    disable_context_linking = bool(nested_config.get("disable_context_linking", False))
    concept_suffixes = tuple(nested_config.get("concept_suffixes", []))
    candidate_terms = {row["term"] for row in candidates}
    context_by_sentence: dict[str, str] = {}
    if analysis_units:
        units_by_document: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for unit in analysis_units:
            units_by_document[str(unit.get("doc_id") or "")].append(unit)
        for document_units in units_by_document.values():
            for index, unit in enumerate(document_units):
                left = max(0, index - context_window)
                right = min(len(document_units), index + context_window + 1)
                context_by_sentence[str(unit.get("sentence_id") or "")] = "\n".join(
                    str(item.get("text") or "") for item in document_units[left:right]
                )
    by_sentence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_term: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for occurrence in occurrences:
        if occurrence["term"] in candidate_terms:
            by_sentence[occurrence["sentence_id"]].append(occurrence)
            by_term[occurrence["term"]].append(occurrence)

    nested_counts: Counter[str] = Counter()
    independent_counts: Counter[str] = Counter()
    independent_patterns: dict[str, Counter[str]] = defaultdict(Counter)
    containers: dict[str, Counter[str]] = defaultdict(Counter)
    nested_sentences: dict[str, set[str]] = defaultdict(set)
    structural_independent_sentences: dict[str, set[str]] = defaultdict(set)
    for sentence_occurrences in by_sentence.values():
        for occurrence in sentence_occurrences:
            term = occurrence["term"]
            longer = {
                other["term"]
                for other in sentence_occurrences
                if other["term"] != term
                and other["start_offset"] <= occurrence["start_offset"]
                and other["end_offset"] >= occurrence["end_offset"]
                and (
                    other["start_offset"] < occurrence["start_offset"]
                    or other["end_offset"] > occurrence["end_offset"]
                )
            }
            if longer:
                nested_counts[term] += 1
                nested_sentences[term].add(str(occurrence["sentence_id"]))
                containers[term].update(longer)
            else:
                independent_counts[term] += 1
                structural_independent_sentences[term].add(str(occurrence["sentence_id"]))
                independent_patterns[term][str(occurrence.get("pos_pattern") or "unknown")] += 1

    kept: list[dict[str, Any]] = []
    nested_rejected = 0
    candidate_total = len(candidates)
    for candidate_index, row in enumerate(candidates, start=1):
        if progress_callback is not None and (
            candidate_index == 1 or candidate_index == candidate_total or candidate_index % 10 == 0
        ):
            progress_callback(candidate_index, candidate_total, "检查嵌套候选与上下文独立性")
        term = row["term"]
        nested = nested_counts[term]
        independent = independent_counts[term]
        dominant = [value for value, _ in containers[term].most_common(5)]
        longer_terms = {longer for longer in candidate_terms if longer != term and term in longer}
        context_linked_sentences = set(nested_sentences[term])
        context_independent_sentences: set[str] = set()
        contextual_containers: Counter[str] = Counter(containers[term])
        for sentence_id in structural_independent_sentences[term]:
            if disable_context_linking:
                context_independent_sentences.add(sentence_id)
                continue
            context = context_by_sentence.get(sentence_id, "")
            linked = {longer for longer in longer_terms if longer in context}
            if linked:
                context_linked_sentences.add(sentence_id)
                contextual_containers.update(linked)
            else:
                context_independent_sentences.add(sentence_id)
        observed_sentences = context_linked_sentences | context_independent_sentences
        long_context_ratio = (
            len(context_linked_sentences) / len(observed_sentences)
            if observed_sentences else 0.0
        )
        contextual_independent = len(context_independent_sentences)
        context_patterns: Counter[str] = Counter()
        for occurrence in by_term[term]:
            if occurrence["term"] == term and occurrence["sentence_id"] in context_independent_sentences:
                context_patterns[str(occurrence.get("pos_pattern") or "unknown")] += 1
        stability = (
            max(context_patterns.values(), default=0) / sum(context_patterns.values())
            if context_patterns else 0.0
        )
        boundary_status = "stable" if contextual_independent and stability >= stability_threshold else "unstable"
        pos_patterns = [value for value in str(row.get("pos_patterns") or "").split("|") if value]
        conceptuality_supported = (
            row.get("trusted") == "yes"
            or "technical_expression" in str(row.get("sources") or "")
            or any("+" in value for value in pos_patterns)
            or (bool(concept_suffixes) and term.endswith(concept_suffixes))
        )
        conceptuality_status = "rule_supported" if conceptuality_supported else "needs_validation"
        if longer_terms and long_context_ratio >= long_context_threshold:
            nested_status = "context_dependent_component"
        elif nested > 0 and independent == 0:
            nested_status = "nested_component"
        elif longer_terms and contextual_independent < minimum_independent:
            nested_status = "needs_review_single_independent"
        elif longer_terms and contextual_independent >= minimum_independent:
            nested_status = "independent_term_validation"
        else:
            nested_status = "not_nested"
        row.update({
            "nested_occurrence_count": nested,
            "independent_occurrence_count": independent,
            "dominant_longer_terms": "|".join(dominant),
            "boundary_stability": round(stability, 4),
            "boundary_status": boundary_status,
            "conceptuality_status": conceptuality_status,
            "nested_validation_status": nested_status,
            "context_linked_sentence_count": len(context_linked_sentences),
            "context_independent_sentence_count": contextual_independent,
            "long_form_context_ratio": round(long_context_ratio, 4),
            "context_dominant_longer_terms": "|".join(
                value for value, _ in contextual_containers.most_common(5)
            ),
        })
        if nested_status in {"nested_component", "context_dependent_component"}:
            nested_rejected += 1
            reason = (
                f"长术语语境占比{long_context_ratio:.2%}达到阈值{long_context_threshold:.0%}，没有形成独立语义环境"
                if nested_status == "context_dependent_component"
                else "所有出现均被更完整候选覆盖，且没有独立出现证据"
            )
            rejected.append({
                **row, "status": "rejected", "candidate_class": nested_status,
                "bcc_count": "", "bcc_status": "not_needed",
                "gate_reasons": reason,
                "rejection_reason": (
                    "long_form_context_dominant"
                    if nested_status == "context_dependent_component"
                    else "fully_nested_component"
                ),
            })
        else:
            kept.append(row)
    return kept, rejected, {"nested_components_removed": nested_rejected}


def apply_boundary_completion_gate(
    candidates: list[dict[str, Any]], rejected: list[dict[str, Any]],
    occurrences: list[dict[str, Any]], token_rows: list[dict[str, Any]],
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Reject candidates whose right edge always stops at a dangling one-character token."""
    tokens_by_sentence = {str(row["sentence_id"]): row["tokens"] for row in token_rows}
    incomplete: dict[str, list[str]] = defaultdict(list)
    total: Counter[str] = Counter()
    occurrence_total = len(occurrences)
    for occurrence_index, occurrence in enumerate(occurrences, start=1):
        if progress_callback is not None and (
            occurrence_index == 1 or occurrence_index == occurrence_total or occurrence_index % 1000 == 0
        ):
            progress_callback(occurrence_index, occurrence_total, "检查候选边界完整性")
        term = str(occurrence["term"])
        total[term] += 1
        tokens = tokens_by_sentence.get(str(occurrence["sentence_id"]), [])
        ending_index = next((
            index for index, token in enumerate(tokens)
            if int(token["end"]) == int(occurrence["end_offset"])
            and int(token["start"]) >= int(occurrence["start_offset"])
            and str(token.get("pos")) != "x"
        ), None)
        if ending_index is None:
            continue
        ending = tokens[ending_index]
        next_token = next((
            token for token in tokens[ending_index + 1:]
            if str(token.get("text") or "").strip()
        ), None)
        if (
            len(str(ending.get("text") or "")) == 1
            and next_token is not None
            and str(next_token.get("pos")) != "x"
            and int(next_token["start"]) == int(occurrence["end_offset"])
        ):
            incomplete[term].append(term + str(next_token["text"]))

    kept: list[dict[str, Any]] = []
    removed = 0
    for row in candidates:
        term = row["term"]
        incomplete_count = len(incomplete[term])
        completion_status = "incomplete" if incomplete_count and incomplete_count == total[term] else "complete"
        row.update({
            "incomplete_boundary_count": incomplete_count,
            "boundary_extensions": "|".join(sorted(set(incomplete[term]))[:5]),
            "boundary_completion_status": completion_status,
        })
        if completion_status == "incomplete":
            removed += 1
            rejected.append({
                **row, "status": "rejected", "candidate_class": "incomplete_boundary",
                "bcc_count": "", "bcc_status": "not_needed",
                "gate_reasons": "所有出现都以可继续构词的单字token结尾，右边界不完整",
                "rejection_reason": "incomplete_right_boundary",
            })
        else:
            kept.append(row)
    return kept, rejected, {"incomplete_boundaries_removed": removed}


def apply_bcc_general_word_gate(
    candidates: list[dict[str, Any]], rejected: list[dict[str, Any]],
    config: dict[str, Any], cache_path: Path, *, offline: bool = False,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    bcc = config.get("bcc_general_frequency", {})
    if not bcc.get("enabled", True):
        for row in candidates:
            row.update({"candidate_class": "trusted_candidate" if row["trusted"] == "yes" else "needs_review", "bcc_count": "", "bcc_status": "disabled", "gate_reasons": ""})
        return candidates, rejected, {"enabled": False, "queried": 0, "cache_hits": 0, "general_words_removed": 0, "errors": []}

    endpoint = str(bcc.get("endpoint", "http://202.112.194.64:18890"))
    channel = str(bcc.get("channel", "multifield"))
    api_key_env = str(bcc.get("api_key_env", "BCC_API_KEY"))
    threshold = int(bcc.get("common_count_threshold", 100000))
    maximum = int(bcc.get("max_queries_per_run", 100000))
    maximum_characters = int(bcc.get("max_query_characters", 8))
    minimum_occurrences = int(bcc.get("minimum_domain_occurrences", 2))
    cached = load_bcc_cache(cache_path, channel)

    eligible = [
        row for row in candidates
        if row["trusted"] == "no"
        and minimum_occurrences <= int(row["occurrence_count"])
        and 2 <= len(row["term"]) <= maximum_characters
        and re.fullmatch(r"[\u3400-\u9fff]+", row["term"])
    ]
    selected_missing = [row["term"] for row in eligible if row["term"] not in cached][:maximum]
    deferred_terms = [row["term"] for row in eligible if row["term"] not in cached][maximum:]
    errors: list[str] = []
    queried_counts: dict[str, int] = {}
    key = os.environ.get(api_key_env, "").strip()
    if selected_missing and key and not offline:
        def persist_batch(batch_counts: dict[str, int]) -> None:
            cached.update(batch_counts)
            save_bcc_cache(cache_path, endpoint=endpoint, channel=channel, counts=cached)

        queried_counts, errors = query_bcc_counts(
            selected_missing, endpoint=endpoint, channel=channel, api_key=key,
            batch_size=int(bcc.get("batch_size", 50)),
            timeout_seconds=int(bcc.get("timeout_seconds", 60)),
            progress_callback=progress_callback,
            batch_result_callback=persist_batch,
        )
        if queried_counts:
            cached.update(queried_counts)
            save_bcc_cache(cache_path, endpoint=endpoint, channel=channel, counts=cached)

    eligible_terms = {row["term"] for row in eligible}
    kept: list[dict[str, Any]] = []
    general_words = 0
    for row in candidates:
        term = row["term"]
        if row["trusted"] == "yes":
            if row.get("nested_validation_status") == "needs_review_single_independent":
                row.update({"candidate_class": "needs_review", "bcc_count": cached.get(term, ""), "bcc_status": "trusted_bypass", "gate_reasons": "短候选仅独立出现1次，仍需独立术语验证"})
            else:
                row.update({"candidate_class": "trusted_candidate", "bcc_count": cached.get(term, ""), "bcc_status": "trusted_bypass", "gate_reasons": "可信词表命中，不以通用频次自动否决"})
            kept.append(row)
            continue
        if term not in eligible_terms:
            if int(row["occurrence_count"]) < minimum_occurrences:
                reason = f"领域出现次数低于BCC查询门槛{minimum_occurrences}"
            elif not 2 <= len(term) <= maximum_characters:
                reason = f"词长不在BCC按需查询范围2～{maximum_characters}字"
            else:
                reason = "不是纯中文候选，未使用中文通用词频门禁"
            row.update({"candidate_class": "needs_review", "bcc_count": cached.get(term, ""), "bcc_status": "not_queried", "gate_reasons": reason})
            kept.append(row)
            continue
        count = cached.get(term)
        if count is None:
            status = "offline_cache_miss" if offline else (
                "api_key_missing" if not key else
                "query_failed" if term in selected_missing else
                "deferred_by_run_budget"
            )
            reason = (
                f"超过本轮BCC新增查询上限{maximum}，下次运行继续查询"
                if status == "deferred_by_run_budget"
                else "尚无BCC通用语料频次，不自动批准或拒绝"
            )
            row.update({"candidate_class": "needs_general_frequency", "bcc_count": "", "bcc_status": status, "gate_reasons": reason})
            kept.append(row)
        elif count >= threshold:
            general_words += 1
            rejected.append({
                **row, "candidate_class": "general_word", "bcc_count": count,
                "bcc_status": "cache" if term not in queried_counts else "queried",
                "gate_reasons": f"BCC通用语料命中数{count}达到通用词风险阈值{threshold}",
                "status": "rejected", "rejection_reason": "bcc_general_frequency_high",
            })
        else:
            nested_status = row.get("nested_validation_status")
            if nested_status == "needs_review_single_independent":
                candidate_class = "needs_review"
                reason = f"BCC风险不高（命中{count}次），但短候选仅独立出现1次"
            elif nested_status == "independent_term_validation" and (
                row.get("boundary_status") != "stable"
                or row.get("conceptuality_status") != "rule_supported"
            ):
                candidate_class = "needs_review"
                reason = (
                    f"BCC风险不高（命中{count}次），但边界稳定性或概念性规则尚未通过"
                )
            else:
                candidate_class = "domain_candidate"
                reason = f"BCC通用语料命中数{count}低于通用词风险阈值{threshold}"
                if nested_status == "independent_term_validation":
                    reason += "；独立出现至少2次、边界稳定且概念性规则支持"
            row.update({
                "candidate_class": candidate_class, "bcc_count": count,
                "bcc_status": "cache" if term not in queried_counts else "queried",
                "gate_reasons": reason,
            })
            kept.append(row)
    return kept, rejected, {
        "enabled": True, "channel": channel, "threshold": threshold,
        "eligible_for_query": len(eligible), "selected_for_query": len(selected_missing), "queried": len(queried_counts),
        "local_run_query_budget": maximum,
        "deferred_by_run_budget": len(deferred_terms),
        "deferred_terms_preview": deferred_terms[:20],
        "query_failures": sum(term not in cached for term in selected_missing),
        "api_access_limit_assumed": None,
        "cache_hits": sum(row["term"] in cached and row["term"] not in queried_counts for row in eligible),
        "cache_misses": sum(row["term"] not in cached for row in eligible),
        "pending_next_run": sum(row["term"] not in cached for row in eligible),
        "scheduler_note": "max_queries_per_run is a local per-run budget, not a BCC API access limit; uncached terms are retried in later runs.",
        "general_words_removed": general_words, "errors": errors,
        "error_count": len(errors), "has_api_errors": bool(errors),
        "credential_source": api_key_env, "credential_persisted": False,
    }


def run_recognition_core(
    input_path: Path, trusted_path: Path, config_path: Path, output_root: Path,
    *, offline: bool = False,
    progress_callback: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    def progress(percent: float, label: str) -> None:
        if progress_callback is not None:
            progress_callback(max(0.0, min(100.0, percent)), label)

    progress(1, "读取冻结输入")
    source_rows = read_jsonl(input_path)
    progress(5, "切分句级分析单元")
    rows, document_count = expand_to_analysis_units(source_rows)
    trusted_terms = read_trusted_terms(trusted_path)
    config = load_recognition_config(config_path)
    progress(10, "加载配置与可信词表")
    occurrences, token_rows, boundary_audit = extract_occurrences(
        rows, config, trusted_terms,
        progress_callback=lambda done, total: progress(
            10 + 45 * done / max(1, total), f"候选生成 {done}/{total}句"
        ),
    )
    progress(58, "聚合全语料候选")
    candidates, rejected = aggregate_candidates(occurrences, config, trusted_terms)
    progress(64, "检查候选边界完整性")
    candidates, rejected, completion_report = apply_boundary_completion_gate(
        candidates, rejected, occurrences, token_rows,
        progress_callback=lambda done, total, label: progress(
            64 + 4 * done / max(1, total), f"{label}：{done}/{total}项"
        ),
    )
    candidates, rejected, nested_report = apply_nested_candidate_gate(
        candidates, rejected, occurrences, config, rows,
        progress_callback=lambda done, total, label: progress(
            68 + 6 * done / max(1, total), f"{label}：{done}/{total}项"
        ),
    )
    if not offline:
        load_local_env(Path(__file__).resolve().parent / ".env")
    progress(74, "准备BCC通用语料查询")
    candidates, rejected, bcc_report = apply_bcc_general_word_gate(
        candidates, rejected, config, output_root / "bcc_frequency_cache.json", offline=offline,
        progress_callback=lambda done, total, label: progress(
            74 + 18 * done / max(1, total), f"{label}：{done}/{total}项"
        ),
    )

    progress(94, "写入识别证据与报告")
    write_jsonl(output_root / "tokens.jsonl", token_rows)
    write_jsonl(output_root / "term_occurrences.jsonl", occurrences)
    candidate_fields = [
        "candidate_id", "term", "occurrence_count", "document_count", "sentence_count",
        "sources", "pos_patterns", "grammar_profiles", "trusted", "score", "representative_contexts",
        "evidence_offsets", "nested_occurrence_count", "independent_occurrence_count",
        "dominant_longer_terms", "boundary_stability", "boundary_status",
        "conceptuality_status", "nested_validation_status", "status",
        "context_linked_sentence_count", "context_independent_sentence_count",
        "long_form_context_ratio", "context_dominant_longer_terms",
        "incomplete_boundary_count", "boundary_extensions", "boundary_completion_status",
        "candidate_class", "bcc_count", "bcc_status", "gate_reasons",
    ]
    write_csv(output_root / "term_candidates.csv", candidate_fields, candidates)
    write_csv(output_root / "term_rejections.csv", candidate_fields + ["rejection_reason"], rejected)
    write_csv(
        output_root / "boundary_audit.csv",
        ["sentence_id", "fragment", "start_offset", "end_offset", "decision", "reason", "sentence"],
        boundary_audit,
    )
    review_rows = [{
        **row, "human_decision": "", "preferred_term": "", "term_definition": "",
        "term_type": "", "reviewer": "", "review_notes": "",
    } for row in candidates]
    write_csv(
        output_root / "term_review_workbench.csv",
        candidate_fields + ["human_decision", "preferred_term", "term_definition", "term_type", "reviewer", "review_notes"],
        review_rows,
    )
    report = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "method": "trusted lexicon + prefix-complete configurable domain grammar traversal + configurable POS correction + technical expressions + corpus aggregation",
        "input": str(input_path), "source_documents": document_count,
        "analysis_sentences": len(rows), "trusted_terms": len(trusted_terms),
        "occurrences": len(occurrences), "candidate_terms": len(candidates),
        "low_evidence_rejections": len(rejected),
        "legacy_fragments_audited": len(boundary_audit),
        "legacy_fragments_rejected": sum(row["decision"] == "rejected" for row in boundary_audit),
        "bcc_general_frequency": bcc_report,
        "nested_candidate_gate": nested_report,
        "boundary_completion_gate": completion_report,
        "candidate_grammar": {
            "default_profile": config.get("candidate_grammar", {}).get("default_profile", "legacy_nominal"),
            "profile_override": config.get("candidate_grammar", {}).get("profile_override", ""),
            "occurrences_by_profile": dict(sorted(Counter(
                str(row.get("grammar_profile") or "unassigned") for row in occurrences
            ).items())),
        },
        "important_note": "All automatic outputs are candidates or rejection recommendations; no term is automatically approved.",
    }
    write_json(output_root / "recognition_report.json", report)
    progress(100, "识别运行完成")
    return report
