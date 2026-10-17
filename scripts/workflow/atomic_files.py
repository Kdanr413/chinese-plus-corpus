"""Bounded retries for Windows readers briefly denying rename/delete sharing."""
import json
import os
import tempfile
import time
from pathlib import Path


def replace_with_retry(source: Path, target: Path, attempts: int = 7) -> None:
    for attempt in range(attempts):
        try:
            os.replace(source,target)
            return
        except PermissionError:
            if attempt+1==attempts:
                raise
            time.sleep(min(0.02*(2**attempt),0.2))


def write_progress_json(target: Path, value: dict) -> None:
    """Unique, closed temporary file; the last valid snapshot is never unlinked."""
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode="w",encoding="utf-8",dir=target.parent,
                prefix=target.name+".",suffix=".tmp",delete=False) as handle:
            temporary=Path(handle.name)
            json.dump(value,handle,ensure_ascii=False,indent=2)
            handle.write("\n");handle.flush();os.fsync(handle.fileno())
        replace_with_retry(temporary,target)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except PermissionError:
                # Leave only this unique temporary file for inspection; never delete the target.
                pass
