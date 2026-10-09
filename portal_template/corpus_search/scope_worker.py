"""Isolated LangSC worker for one verified corpus scope.

LangSC.BCC 2.x keeps the loaded native index in process-global state.  A fresh
process is therefore the reliable boundary when a server switches between the
main index and per-scope indexes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .engine import load_corpus


MARKER = "__CORPUS_SCOPE_RESULT__="


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        # The JSON control channel is UTF-8 even when Windows' console code page
        # is GBK.  This does not read or rewrite corpus/index asset bytes.
        if hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8", errors="strict")
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="strict")
        if len(arguments) != 1:
            raise ValueError("expected one scope index directory")
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
        query = request.get("query")
        command = request.get("command", "Freq")
        options = request.get("options", {})
        if not isinstance(options, dict):
            raise ValueError("options must be a JSON object")
        engine = load_corpus(Path(arguments[0]))
        result = engine.search(query, command=command, **options)
        envelope = {"ok": True, "result": result}
        status = 0
    except Exception as exc:  # process boundary: return a stable error envelope
        envelope = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        status = 1
    print(MARKER + json.dumps(envelope, ensure_ascii=False))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
