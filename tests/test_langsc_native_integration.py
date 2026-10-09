"""Opt-in parity tests against the installed LangSC 2.0.7 native BCC engine.

Run with ``LANGSC_NATIVE_INTEGRATION=1``.  The fallback loader exists only for
this development machine, whose Anaconda pywin32 installation is incomplete;
production code continues to use the normal ``from LangSC import BCC`` import.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

from corpus_search import build_index, build_scope_indexes, create_corpus_selection, load_corpus


_langsc_spec = importlib.util.find_spec("LangSC")
LANGSC_DIR = Path(
    os.environ.get("LANGSC_PACKAGE_DIR")
    or (
        str(Path(_langsc_spec.origin).resolve().parent)
        if _langsc_spec is not None and _langsc_spec.origin
        else "LangSC"
    )
)


def load_bcc_class():
    try:
        from LangSC import BCC

        return BCC
    except Exception:
        pass

    win32api = types.ModuleType("win32api")
    win32api.FreeLibrary = lambda _handle: None
    sys.modules["win32api"] = win32api

    chardet = types.ModuleType("chardet")

    def detect(raw: bytes) -> dict[str, str]:
        try:
            raw.decode("utf-8")
            return {"encoding": "utf-8"}
        except UnicodeDecodeError:
            return {"encoding": "GB2312"}

    chardet.detect = detect
    sys.modules["chardet"] = chardet

    package = types.ModuleType("LangSC")
    package.__path__ = [str(LANGSC_DIR)]
    package.__package__ = "LangSC"
    sys.modules["LangSC"] = package
    for name in ("_utils", "bcc"):
        spec = importlib.util.spec_from_file_location(
            f"LangSC.{name}", LANGSC_DIR / f"{name}.py"
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load LangSC.{name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return sys.modules["LangSC.bcc"].BCC


def snapshot(path: Path) -> dict[str, tuple[int, str]]:
    return {
        item.relative_to(path).as_posix(): (
            item.stat().st_size,
            hashlib.sha256(item.read_bytes()).hexdigest(),
        )
        for item in path.rglob("*")
        if item.is_file()
    }


@unittest.skipUnless(
    os.environ.get("LANGSC_NATIVE_INTEGRATION") == "1",
    "set LANGSC_NATIVE_INTEGRATION=1 to run the native LangSC parity test",
)
class LangSCNativeParityTests(unittest.TestCase):
    def test_scope_indexes_build_in_isolated_workers(self) -> None:
        BCC = load_bcc_class()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            sources = []
            for number, word in enumerate(("动力电池", "光伏发电"), start=1):
                source = root / f"corpus-{number}.txt"
                source.write_text(
                    f"Doc 原生范围测试{number}\n"
                    f"Item: (ROOT(NP(n {word}))(VP(v 发展))(w(x 。)))\n",
                    encoding="gb18030",
                )
                sources.append(source)
            index = root / "index"
            build_index(index, sources, backend_factory=BCC)
            result = build_scope_indexes(index, sources)
            self.assertEqual(result.scope_count, 2)
            self.assertTrue(result.manifest_path.is_file())
            engine = load_corpus(index, backend_factory=BCC)
            scopes = engine.available_scopes()
            scoped = engine.count("动力电池", scope_ranges=[scopes[0].query_range])
            self.assertGreater(scoped["Freq"][0], 0)

    def test_old_and_adapter_queries_match_in_isolated_processes(self) -> None:
        BCC = load_bcc_class()
        # LangSC 2.0.7 keeps Engine.txt open until this Python process exits;
        # Windows therefore cannot remove the temporary index in __exit__.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            source = root / "corpus.txt"
            source.write_text(
                "Doc 阶段三用于验证特殊结构标记不会变化的测试语料文档甲\n"
                "#Global 语种=汉语;语料领域=新能源;文体=说明\n"
                "#Group 术语=电池;术语=充电\n"
                "Item: (ROOT(CON(x ()(m 一)(x )))(NP(n 新能源)(n 电池))(VP(v 支持))(NP(a 快速)(v 充电))(w(x 。)))\n"
                "#Global 语种=汉语;语料领域=教学;文体=课堂\n"
                "#Group 术语=课程\n"
                "Item: (ROOT(NP(n 中文)(n 课堂))(VP(v 介绍))(NP(n 新能源)(n 技术))(w(x 。)))\n",
                encoding="gb18030",
            )
            source_before = hashlib.sha256(source.read_bytes()).hexdigest()
            catalog = root / "index_items.jsonl"
            catalog.write_text(
                json.dumps(
                    {
                        "item_id": "item-a",
                        "doc_id": "doc-a",
                        "doc_name": "阶段三用于验证特殊结构标记不会变化的测试语料文档甲",
                    },
                    ensure_ascii=False,
                )
                + "\n"
                + json.dumps(
                    {
                        "item_id": "item-b",
                        "doc_id": "doc-a",
                        "doc_name": "阶段三用于验证特殊结构标记不会变化的测试语料文档甲",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            selection = create_corpus_selection(
                source,
                root / "selection",
                catalog_path=catalog,
                item_ids=["item-a", "item-b"],
                output_encoding="gbk",
            )
            self.assertEqual(selection.normalized_parenthesis_tokens, 2)
            self.assertEqual(len(selection.corpus_files), 1)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_before)

            filelist = root / "filelist.txt"
            filelist.write_text(
                "\n".join(path.resolve().as_posix() for path in selection.corpus_files)
                + "\n",
                encoding="ascii",
            )
            new_index = root / "new-index"
            build = build_index(
                new_index,
                filelist,
                backend_factory=BCC,
            )
            self.assertGreater(build.status_code, 0)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_before)
            before = snapshot(new_index)

            query_script = r'''
import json
import sys
from corpus_search import load_corpus
from tests.test_langsc_native_integration import load_bcc_class

index_path, mode = sys.argv[1:]
BCC = load_bcc_class()
if mode == "old":
    engine = BCC(index_path)
    engine.dataPath = index_path
    result = {
        "Freq": json.loads(engine.Run("n", Command="Freq", Number=20)),
        "Context": json.loads(engine.Run("n", Command="Context", PageNo=0, WinSize=20, Number=20)),
        "Count": json.loads(engine.Run("n", Command="Count")),
        "PatternCount": json.loads(engine.Run("a的n", Command="Count")),
    }
else:
    engine = load_corpus(index_path, backend_factory=BCC)
    result = {
        "Freq": engine.frequency("n", Number=20),
        "Context": engine.kwic("n", PageNo=0, WinSize=20, Number=20),
        "Count": engine.count("n"),
        "PatternCount": engine.count("a的n"),
    }
print("__RESULT__" + json.dumps(result, ensure_ascii=False))
'''

            def isolated_query(mode: str) -> dict[str, object]:
                completed = subprocess.run(
                    [sys.executable, "-c", query_script, new_index.as_posix(), mode],
                    cwd=Path(__file__).resolve().parents[1],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=True,
                    env=os.environ.copy(),
                )
                marker = next(
                    line for line in completed.stdout.splitlines()
                    if line.startswith("__RESULT__")
                )
                return json.loads(marker.removeprefix("__RESULT__"))

            expected = isolated_query("old")
            actual = isolated_query("adapter")
            self.assertEqual(actual, expected)
            self.assertGreater(expected["Count"]["Freq"][0], 0)
            self.assertGreater(expected["Freq"]["Count"], 0)
            self.assertGreater(expected["Context"]["Count"], 0)
            self.assertEqual(snapshot(new_index), before)


if __name__ == "__main__":
    unittest.main()
