"""Build recognition units without changing the persisted cleaned corpus format."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, Sequence

from .models import Document


DIALOGUE_TURN = re.compile(
    r"^\s*(?:[（(]?([\u3400-\u9fffA-Za-z][\u3400-\u9fffA-Za-z0-9·._\-]{0,19})[）)]?)\s*[：:]\s*\S+"
)
SENTENCE = re.compile(r".+?(?:[。！？!?；;]+[”’）)]*|$)")


def _unit(parent: Document, index: int, unit_type: str, text: str, **metadata: object) -> Document:
    text = text.replace("\x00", "")
    unit_id = f"{parent.document_id}__{'turn' if unit_type == 'dialogue_turn' else 'sentence'}_{index:06d}"
    return Document(
        document_id=unit_id,
        source=parent.source,
        raw_text=text,
        clean_text=text,
        processing_version="recognition-unit/1.0",
        metadata={
            **parent.metadata,
            "source_document_id": parent.document_id,
            "analysis_unit_id": unit_id,
            "analysis_unit_type": unit_type,
            "analysis_unit_index": index,
            **metadata,
        },
    )


def _dialogue_turns(document: Document) -> list[Document]:
    turns: list[tuple[str, str]] = []
    speaker = ""
    buffer: list[str] = []
    for block in re.split(r"\n\s*\n|\r?\n", document.clean_text):
        text = block.strip()
        if not text:
            continue
        match = DIALOGUE_TURN.match(text)
        if match:
            if buffer:
                turns.append((speaker, "\n".join(buffer)))
            speaker = match.group(1).strip()
            buffer = [text]
        elif buffer:
            buffer.append(text)
    if buffer:
        turns.append((speaker, "\n".join(buffer)))
    return [
        _unit(document, index, "dialogue_turn", text, speaker=current_speaker)
        for index, (current_speaker, text) in enumerate(turns, start=1)
    ]


def _fallback_sentences(document: Document) -> list[Document]:
    rows = [match.group(0).strip() for match in SENTENCE.finditer(document.clean_text)]
    return [
        _unit(document, index, "sentence", text)
        for index, text in enumerate(rows, start=1)
        if text
    ]


def _sentence_rows(path: Path | None) -> dict[str, list[dict[str, object]]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    if path is None or not path.is_file():
        return grouped
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("is_heading") or not str(row.get("text", "")).strip():
                continue
            grouped.setdefault(str(row.get("doc_id", "")), []).append(row)
    return grouped


def build_recognition_units(
    documents: Sequence[Document], *, sentences_path: str | Path | None = None
) -> list[Document]:
    """Return sentence units for narratives and complete speaker turns for dialogues."""
    stored_sentences = _sentence_rows(Path(sentences_path) if sentences_path else None)
    units: list[Document] = []
    for document in documents:
        genre = str(document.metadata.get("text_genre") or "记叙文")
        if genre == "对话":
            dialogue = _dialogue_turns(document)
            units.extend(dialogue or _fallback_sentences(document))
            continue
        rows = sorted(
            stored_sentences.get(document.document_id, []),
            key=lambda row: int(row.get("sentence_index", 0)),
        )
        if rows:
            units.extend(
                _unit(
                    document,
                    index,
                    "sentence",
                    str(row["text"]).strip(),
                    source_sentence_id=str(row.get("sentence_id", "")),
                    paragraph_id=str(row.get("paragraph_id", "")),
                    section_id=str(row.get("section_id", "")),
                    section_path=row.get("section_path", []),
                )
                for index, row in enumerate(rows, start=1)
            )
        else:
            units.extend(_fallback_sentences(document))
    return units
