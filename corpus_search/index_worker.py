"""Build one native BCC index in an isolated process.

LangSC 2.0.7 stores mutable state in its native DLL.  This worker is kept
small on purpose so the DLL is unloaded after every scope index build.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .engine import build_index


MARKER = "__CORPUS_INDEX_RESULT__="


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print(MARKER + json.dumps({"ok": False, "error": "expected index path"}))
        return 2
    index_dir = Path(args[0])
    try:
        request = json.loads(sys.stdin.read())
        raw_sources = request.get("sources")
        structure = str(request.get("structure", "HZ"))
        if not isinstance(raw_sources, list) or not raw_sources:
            raise ValueError("sources must be a non-empty list")
        result = build_index(
            index_dir,
            [Path(str(source)) for source in raw_sources],
            structure=structure,
        )
        payload = {
            "ok": True,
            "status_code": result.status_code,
            "files": list(result.files),
        }
        print(MARKER + json.dumps(payload, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(MARKER + json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
