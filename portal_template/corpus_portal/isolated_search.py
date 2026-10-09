"""Run native BCC in a fresh process: its initialization is process-global."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from corpus_search import CorpusQueryError, CorpusSearchDependencyError
from .workspaces import ROOT

MARKER = "__CORPUS_QUERY_RESULT__"

class IsolatedSearchEngine:
    def __init__(self, index_dir, slots=None):
        self.index_dir = Path(index_dir).resolve()
        self.slots = slots or threading.BoundedSemaphore(4)

    def _query(self, method, query, options):
        if not self.slots.acquire(timeout=30):
            raise CorpusSearchDependencyError("Shared search is busy; retry later")
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            env = dict(os.environ, PYTHONUTF8="1")
            env.pop("PYTHONPATH", None)
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "corpus_portal.isolated_search"],
                    input=json.dumps({"index": str(self.index_dir), "method": method,
                                      "query": query, "options": options}, ensure_ascii=False),
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=60, cwd=ROOT, env=env, creationflags=flags,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise CorpusSearchDependencyError("Shared search worker unavailable") from exc
            lines = [line[len(MARKER):] for line in result.stdout.splitlines() if line.startswith(MARKER)]
            if result.returncode or not lines:
                raise CorpusSearchDependencyError("Shared search worker failed")
            try:
                payload = json.loads(lines[-1])
            except ValueError as exc:
                raise CorpusSearchDependencyError("Invalid shared search worker response") from exc
            if not payload.get("ok"):
                if payload.get("query_error"):
                    raise CorpusQueryError("Invalid BCC query")
                raise CorpusSearchDependencyError("Shared search index unavailable")
            return payload["result"]
        finally:
            self.slots.release()

    def frequency(self, query, **options):
        return self._query("frequency", query, options)

    def kwic(self, query, **options):
        return self._query("kwic", query, options)


def main():
    from corpus_search import load_corpus
    try:
        payload = json.load(sys.stdin)
        if payload["method"] not in {"frequency", "kwic"}:
            raise CorpusQueryError("Unsupported query method")
        engine = load_corpus(payload["index"])
        result = getattr(engine, payload["method"])(payload["query"], **payload.get("options", {}))
        response = {"ok": True, "result": result}
    except Exception as exc:
        response = {"ok": False, "query_error": isinstance(exc, CorpusQueryError)}
    print(MARKER + json.dumps(response, ensure_ascii=False), flush=True)

if __name__ == "__main__":
    main()
