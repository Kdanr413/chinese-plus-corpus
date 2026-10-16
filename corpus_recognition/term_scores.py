"""Preserve extraction scores and hydrate legacy downloads without rewriting saved artifacts."""
import csv
import json
import math
import os
import threading
import uuid
from pathlib import Path

_LOCK=threading.Lock()


def numeric_score(value):
    if value is None or isinstance(value,bool) or not str(value).strip():
        return None
    try:
        result=float(value)
        return result if math.isfinite(result) else None
    except (TypeError,ValueError):
        return None


def term_metadata(rows):
    """Stable term identity, highest available extraction score across occurrences."""
    result={}
    for row in rows:
        term=str(row.get("normalized_form") or "").strip()
        if not term:
            continue
        current=result.setdefault(term,dict(row))
        value=numeric_score(row.get("score"));previous=numeric_score(current.get("score"))
        if value is not None and (previous is None or value>previous):
            current["score"]=row["score"]
    return result


def terminology_with_scores(path: Path) -> Path:
    """Export a separate scored CSV, including provenance when no score exists."""
    table=path.parents[1]/"05_term_validation/term_validation_table.csv"
    target=path.with_name("terminology_with_scores.csv")
    manifest=target.with_suffix(".manifest.json")
    sources=[source for source in (path,table) if source.is_file()]
    stamp=[{"name":source.name,"bytes":source.stat().st_size,"mtime_ns":source.stat().st_mtime_ns} for source in sources]
    with _LOCK:
        if target.exists() and manifest.exists():
            try:
                if json.loads(manifest.read_text(encoding="utf-8"))==stamp:
                    return target
            except (ValueError,OSError):
                pass
        with path.open(encoding="utf-8-sig",newline="") as handle:
            reader=csv.DictReader(handle);fields=list(reader.fieldnames or []);rows=list(reader)
        for field in ("score","score_source","score_status"):
            if field not in fields:
                fields.append(field)
        missing={str(row.get("preferred_term") or row.get("term") or "").strip() for row in rows if numeric_score(row.get("score")) is None}
        scores={}
        if missing and table.is_file():
            with table.open(encoding="utf-8-sig",newline="") as handle:
                for row in csv.DictReader(handle):
                    term=str(row.get("normalized_form") or "").strip()
                    value=numeric_score(row.get("score"))
                    if term in missing and value is not None and (term not in scores or value>scores[term][0]):
                        scores[term]=(value,row["score"])
        temporary=target.with_name(target.name+"."+uuid.uuid4().hex+".tmp")
        try:
            with temporary.open("w",encoding="utf-8-sig",newline="") as handle:
                writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader()
                for row in rows:
                    term=str(row.get("preferred_term") or row.get("term") or "").strip()
                    if numeric_score(row.get("score")) is not None:
                        row["score_source"]="saved_terminology"
                    elif term in scores:
                        row["score"]=scores[term][1];row["score_source"]="saved_term_validation_max"
                    else:
                        row["score"]="";row["score_source"]="not_recorded"
                    row["score_status"]="available" if numeric_score(row.get("score")) is not None else "not_recorded"
                    writer.writerow(row)
                handle.flush();os.fsync(handle.fileno())
            temporary.replace(target)
            meta=manifest.with_suffix(".json.tmp");meta.write_text(json.dumps(stamp),encoding="utf-8");meta.replace(manifest)
        finally:
            temporary.unlink(missing_ok=True)
    return target
