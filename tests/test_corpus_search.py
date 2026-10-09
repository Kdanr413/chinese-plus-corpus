from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from corpus_search import (
    CorpusIndexError,
    CorpusIndexExistsError,
    CorpusQueryError,
    CorpusSelectionError,
    CorpusSelectionExistsError,
    OrgScope,
    build_index,
    build_scope_indexes,
    create_corpus_selection,
    load_corpus,
    read_org_scopes,
)


class FakeBCC:
    active_calls = 0
    max_active_calls = 0
    activity_lock = threading.Lock()
    last_index_sources: object = None

    def __init__(self, data_path: str) -> None:
        self.dataPath = data_path
        self.calls: list[tuple[str, dict[str, object]]] = []

    def IndexBCC(self, sources: object, **options: object) -> int:
        type(self).last_index_sources = sources
        target = Path(self.dataPath)
        target.mkdir(parents=True, exist_ok=True)
        for name in ("IdxUnit.dat", "DatWord.dat", "Org.dat", "SentInfo.dat"):
            (target / name).write_bytes(name.encode("ascii"))
        (target / "Org.txt").write_text(
            "#0\t1\nT:fixture\nEx:\n", encoding="gb18030"
        )
        (target / "options.json").write_text(
            json.dumps(options, sort_keys=True), encoding="utf-8"
        )
        return 1

    def Run(self, query: str, **options: object) -> str:
        self.calls.append((query, options))
        with self.activity_lock:
            type(self).active_calls += 1
            type(self).max_active_calls = max(
                type(self).max_active_calls, type(self).active_calls
            )
        time.sleep(0.005)
        with self.activity_lock:
            type(self).active_calls -= 1
        return json.dumps(
            {"Type": options["Command"], "Query": query, "Options": options},
            ensure_ascii=False,
        )

    def CallBCC(self, query: str) -> str:
        return f"raw:{query}"

    def AddBCCKV(self, key: str, value: str) -> str:
        return f"add:{key}:{value}"

    def GetBCCKV(self, key: str = "") -> str:
        return f"get:{key}"

    def ClearBCCKV(self, key: str = "") -> str:
        return "clear:all"


def make_index(path: Path) -> None:
    path.mkdir(parents=True)
    for name in ("IdxUnit.dat", "DatWord.dat", "Org.dat", "SentInfo.dat"):
        (path / name).write_bytes(name.encode("ascii"))


class CorpusSearchAdapterTests(unittest.TestCase):
    def test_load_corpus_requires_a_complete_existing_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            index_dir = Path(directory) / "missing"
            with self.assertRaises(CorpusIndexError):
                load_corpus(index_dir, backend_factory=FakeBCC)

            index_dir.mkdir()
            (index_dir / "IdxUnit.dat").write_bytes(b"partial")
            with self.assertRaises(CorpusIndexError):
                load_corpus(index_dir, backend_factory=FakeBCC)

    def test_load_uses_absolute_path_and_parses_json_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            index_dir = Path(directory) / "index"
            make_index(index_dir)
            engine = load_corpus(index_dir, backend_factory=FakeBCC)

            self.assertEqual(engine.index_dir, index_dir.resolve())
            self.assertEqual(engine._backend.dataPath, index_dir.resolve().as_posix())
            self.assertEqual(engine.frequency("新能源")["Type"], "Freq")
            self.assertEqual(engine.kwic("新能源", Number=5)["Type"], "Context")
            self.assertEqual(engine.count("新能源")["Type"], "Count")
            self.assertEqual(
                json.loads(engine.raw_search("新能源")),
                engine.frequency("新能源"),
            )

    def test_quick_reference_parameters_and_raw_apis_are_mapped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            index_dir = Path(directory) / "index"
            make_index(index_dir)
            engine = load_corpus(index_dir, backend_factory=FakeBCC)

            frequency = engine.frequency(
                "新能源", number=8, target="$1", context_num=2
            )
            self.assertEqual(
                frequency["Options"],
                {
                    "Command": "Freq",
                    "Number": 8,
                    "Target": "$1",
                    "ContextNum": 2,
                },
            )
            context = engine.kwic(
                "新能源", number=6, page_no=2, win_size=30
            )
            self.assertEqual(
                context["Options"],
                {"Command": "Context", "Number": 6, "PageNo": 2, "WinSize": 30},
            )
            self.assertEqual(engine.call_bcc("n{}Count()"), "raw:n{}Count()")
            self.assertEqual(
                engine.add_bcc_kv("领域词", "电池;充电"),
                "add:领域词:电池;充电",
            )
            self.assertEqual(engine.get_bcc_kv("领域词"), "get:领域词")
            self.assertEqual(engine.clear_bcc_kv(), "clear:all")
            with self.assertRaises(CorpusQueryError):
                engine.clear_bcc_kv("领域词")

            with self.assertRaisesRegex(CorpusQueryError, "scope_indexes.json"):
                engine.frequency("新能源", scope_ranges=[(0, 62)])

    def test_build_index_is_explicit_and_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "corpus.txt"
            source.write_text("Doc test\nItem:测试\n", encoding="gb18030")
            index_dir = root / "release-index"

            result = build_index(
                index_dir,
                [source],
                structure="HZ",
                backend_factory=FakeBCC,
            )
            self.assertEqual(result.status_code, 1)
            self.assertIn("IdxUnit.dat", result.files)
            self.assertEqual(FakeBCC.last_index_sources, [source.resolve().as_posix()])
            self.assertEqual(
                json.loads((index_dir / "options.json").read_text(encoding="utf-8")),
                {"Structure": "HZ"},
            )

            with self.assertRaises(CorpusIndexExistsError):
                build_index(index_dir, [source], backend_factory=FakeBCC)

    def test_existing_file_list_remains_a_file_list_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "corpus.txt"
            source.write_text("Doc test\nItem:测 试\n", encoding="gb18030")
            filelist = root / "filelist.txt"
            filelist.write_text(source.resolve().as_posix() + "\n", encoding="utf-8")
            build_index(
                root / "index",
                filelist,
                backend_factory=FakeBCC,
            )
            self.assertEqual(FakeBCC.last_index_sources, filelist.resolve().as_posix())

    def test_query_validation_and_invalid_native_json_are_typed_errors(self) -> None:
        class InvalidJSONBCC(FakeBCC):
            def Run(self, query: str, **options: object) -> str:
                return "not-json"

        with tempfile.TemporaryDirectory() as directory:
            index_dir = Path(directory) / "index"
            make_index(index_dir)
            engine = load_corpus(index_dir, backend_factory=InvalidJSONBCC)
            with self.assertRaises(CorpusQueryError):
                engine.search("")
            with self.assertRaises(CorpusQueryError):
                engine.search("测试")
            with self.assertRaises(CorpusQueryError):
                engine.search("测试", Command="Count")

    def test_org_ranges_are_read_for_human_scope_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            index_dir = Path(directory) / "index"
            make_index(index_dir)
            (index_dir / "Org.txt").write_text(
                "#0\t62\nT:教材甲\nEx:\n#62\t100\nT:报告乙\nEx:\n",
                encoding="gb18030",
            )
            expected = (
                OrgScope(0, 62, "教材甲", ("Ex:",)),
                OrgScope(62, 100, "报告乙", ("Ex:",)),
            )
            self.assertEqual(read_org_scopes(index_dir), expected)
            engine = load_corpus(index_dir, backend_factory=FakeBCC)
            self.assertEqual(engine.available_scopes(), expected)

    def test_verified_scope_routes_to_worker_and_all_scopes_use_main(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            index_dir = Path(directory) / "index"
            make_index(index_dir)
            (index_dir / "Org.txt").write_text(
                "#0\t2\nT:教材甲\nEx:\n#2\t5\nT:报告乙\nEx:\n"
                "#5\t8\nT:新闻丙\nEx:\n",
                encoding="gb18030",
            )
            for number in (1, 2, 3):
                child = index_dir / "scope_indexes" / f"scope-{number:04d}"
                make_index(child)
            (index_dir / "scope_indexes.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "scopes": [
                            {"start": 0, "end": 2, "title": "教材甲", "index_path": "scope_indexes/scope-0001"},
                            {"start": 2, "end": 5, "title": "报告乙", "index_path": "scope_indexes/scope-0002"},
                            {"start": 5, "end": 8, "title": "新闻丙", "index_path": "scope_indexes/scope-0003"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            engine = load_corpus(index_dir, backend_factory=FakeBCC)
            with patch(
                "corpus_search.engine._run_scope_worker",
                return_value={"Type": "Count", "Total": 7},
            ) as worker:
                self.assertEqual(
                    engine.count("新能源", scope_ranges=[(2, 5)])["Total"], 7
                )
                self.assertEqual(
                    worker.call_args.args[0],
                    (index_dir / "scope_indexes" / "scope-0002").resolve(),
                )
            whole = engine.count(
                "新能源", scope_ranges=[(0, 2), (2, 5), (5, 8)]
            )
            self.assertEqual(whole["Query"], "新能源")
            with self.assertRaisesRegex(CorpusQueryError, "partial multi-scope"):
                engine.count("新能源", scope_ranges=[(0, 2), (2, 5)])
            with self.assertRaisesRegex(CorpusQueryError, "not registered"):
                engine.count("新能源", scope_ranges=[(9, 10)])

    def test_build_scope_indexes_maps_sources_and_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main = root / "index"
            make_index(main)
            (main / "Org.txt").write_text(
                "#0\t1\nT:甲\nEx:\n#1\t2\nT:乙\nEx:\n", encoding="gb18030"
            )
            sources = []
            for name in ("a.txt", "b.txt"):
                source = root / name
                source.write_text("Doc fixture\nItem:测试\n", encoding="gb18030")
                sources.append(source)
            result = build_scope_indexes(main, sources, backend_factory=FakeBCC)
            self.assertEqual(result.scope_count, 2)
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [entry["source_name"] for entry in manifest["scopes"]],
                ["a.txt", "b.txt"],
            )
            with self.assertRaises(CorpusIndexExistsError):
                build_scope_indexes(main, sources, backend_factory=FakeBCC)

    def test_native_scope_builds_are_isolated_in_workers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main = root / "index"
            make_index(main)
            (main / "Org.txt").write_text(
                "#0\t1\nT:甲\nEx:\n#1\t2\nT:乙\nEx:\n", encoding="gb18030"
            )
            sources = []
            for name in ("a.txt", "b.txt"):
                source = root / name
                source.write_text("Doc fixture\nItem:测试\n", encoding="gb18030")
                sources.append(source)

            def isolated_builder(index_dir: Path, _source: Path, _structure: str) -> None:
                FakeBCC(str(index_dir)).IndexBCC([])

            with patch(
                "corpus_search.engine._build_scope_worker",
                side_effect=isolated_builder,
            ) as worker:
                result = build_scope_indexes(main, sources)

            self.assertEqual(result.scope_count, 2)
            self.assertEqual(worker.call_count, 2)

    def test_build_rejects_utf8_corpus_and_accepts_gb18030(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            utf8 = root / "utf8.txt"
            utf8.write_text("Doc 测试文档\nItem: 新能源\n", encoding="utf-8")
            with self.assertRaisesRegex(CorpusIndexError, "UTF-8"):
                build_index(root / "utf8-index", utf8, backend_factory=FakeBCC)

            gb18030 = root / "gb18030.txt"
            gb18030.write_text(
                "Doc 测试文档\nItem: 新能源\n", encoding="gb18030"
            )
            result = build_index(
                root / "gb18030-index", gb18030, backend_factory=FakeBCC
            )
            self.assertEqual(result.status_code, 1)

    def test_native_calls_are_serialized_for_concurrent_readers(self) -> None:
        FakeBCC.active_calls = 0
        FakeBCC.max_active_calls = 0
        with tempfile.TemporaryDirectory() as directory:
            index_dir = Path(directory) / "index"
            make_index(index_dir)
            engine = load_corpus(index_dir, backend_factory=FakeBCC)
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(engine.count, ["新能源"] * 24))

            self.assertTrue(all(result["Type"] == "Count" for result in results))
            self.assertEqual(FakeBCC.max_active_calls, 1)


class CorpusSelectionTests(unittest.TestCase):
    def write_fixture(self, root: Path) -> tuple[Path, Path, list[dict[str, str]]]:
        source = root / "complete.txt"
        source.write_text(
            "Doc 教材甲\n"
            "#Global 文体=对话;话题=介绍与说明\n"
            "#Group 术语=电池\n"
            "Item: (ROOT(x ()(n 电池)(x )))\n"
            "#Global 文体=对话;话题=操作与程序\n"
            "#Group 术语=充电\n"
            "Item: (ROOT(v 充电))\n"
            "Doc 报告乙\n"
            "#Global 文体=记叙文;话题=新能源\n"
            "#Group\n"
            "Item: (ROOT(n 波浪能))\n",
            encoding="utf-8",
        )
        rows = [
            {"item_id": "item-1", "doc_id": "doc-a", "doc_name": "教材甲"},
            {"item_id": "item-2", "doc_id": "doc-a", "doc_name": "教材甲"},
            {"item_id": "item-3", "doc_id": "doc-b", "doc_name": "报告乙"},
        ]
        catalog = root / "index_items.jsonl"
        catalog.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        return source, catalog, rows

    def test_selects_stable_item_ids_without_changing_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, catalog, _ = self.write_fixture(root)
            before = source.read_bytes()

            result = create_corpus_selection(
                source,
                root / "selected",
                catalog_path=catalog,
                item_ids=["item-1", "item-3"],
            )

            self.assertEqual(result.total_items, 3)
            self.assertEqual(result.selected_items, 2)
            self.assertEqual(result.selected_item_ids, ("item-1", "item-3"))
            self.assertEqual(result.selected_documents, ("教材甲", "报告乙"))
            self.assertEqual(len(result.corpus_files), 2)
            self.assertEqual(source.read_bytes(), before)
            selected = [path.read_text(encoding="gb18030") for path in result.corpus_files]
            self.assertIn("Doc bcc_document_0001_doc-a", selected[0])
            self.assertIn("Item: (ROOT(x （)(n 电池)(x ）))", selected[0])
            self.assertNotIn("Item: (ROOT(v 充电))", selected[0])
            self.assertIn("Doc bcc_document_0002_doc-b", selected[1])
            self.assertIn("Item: (ROOT(n 波浪能))", selected[1])
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["selected_item_ids"], ["item-1", "item-3"])
            self.assertEqual(
                manifest["corpus_files"][0]["bcc_document_id"],
                "bcc_document_0001_doc-a",
            )
            self.assertEqual(result.normalized_parenthesis_tokens, 2)
            self.assertEqual(result.output_encoding, "gb18030")
            self.assertEqual(manifest["output_encoding"], "gb18030")
            self.assertEqual(
                manifest["bcc_compatibility"]["normalized_token_count"], 2
            )
            self.assertEqual(
                [entry["sha256"] for entry in manifest["corpus_files"]],
                [entry[1] for entry in result.corpus_sha256],
            )

    def test_document_and_metadata_filters_are_combined_with_and(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, catalog, _ = self.write_fixture(root)
            result = create_corpus_selection(
                source,
                root / "selected",
                catalog_path=catalog,
                document_names="教材甲",
                global_filters={"话题": "操作与程序"},
                group_filters={"术语": ["充电", "换电"]},
            )
            self.assertEqual(result.selected_items, 1)
            self.assertEqual(result.selected_item_ids, ("item-2",))
            self.assertEqual(len(result.corpus_files), 1)
            selected = result.corpus_files[0].read_text(encoding="gb18030")
            self.assertIn("#Global 文体=对话;话题=操作与程序", selected)
            self.assertIn("#Group 术语=充电", selected)

            index = build_index(
                root / "selected-index",
                result.corpus_files,
                backend_factory=FakeBCC,
            )
            self.assertEqual(index.status_code, 1)

    def test_gbk_output_is_available_for_langsc_2_0_7(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, catalog, _ = self.write_fixture(root)
            result = create_corpus_selection(
                source,
                root / "selected-gbk",
                catalog_path=catalog,
                output_encoding="gbk",
            )

            selected = result.corpus_files[0].read_text(encoding="gbk")
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertIn("Item: (ROOT", selected)
            self.assertEqual(result.output_encoding, "gbk")
            self.assertEqual(manifest["output_encoding"], "gbk")

            with self.assertRaises(CorpusSelectionError):
                create_corpus_selection(
                    source,
                    root / "selected-utf8",
                    output_encoding="utf-8",
                )

    def test_bcc_parenthesis_normalization_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, catalog, _ = self.write_fixture(root)
            result = create_corpus_selection(
                source,
                root / "selected",
                catalog_path=catalog,
                item_ids=["item-1"],
                normalize_bcc_parentheses=False,
            )
            selected = result.corpus_files[0].read_text(encoding="gb18030")
            self.assertIn("Item: (ROOT(x ()(n 电池)(x )))", selected)
            self.assertEqual(result.normalized_parenthesis_tokens, 0)

    def test_selection_refuses_zero_matches_and_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, catalog, _ = self.write_fixture(root)
            with self.assertRaises(CorpusSelectionError):
                create_corpus_selection(
                    source,
                    root / "none",
                    catalog_path=catalog,
                    item_ids=["missing"],
                )

            output = root / "existing"
            output.mkdir()
            (output / "file.txt").write_text("already here", encoding="utf-8")
            with self.assertRaises(CorpusSelectionExistsError):
                create_corpus_selection(source, output, catalog_path=catalog)

    def test_item_ids_require_a_matching_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, catalog, rows = self.write_fixture(root)
            with self.assertRaises(CorpusSelectionError):
                create_corpus_selection(
                    source,
                    root / "missing-catalog.txt",
                    item_ids=["item-1"],
                )

            catalog.write_text(
                json.dumps(rows[0], ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(CorpusSelectionError):
                create_corpus_selection(
                    source,
                    root / "bad-catalog.txt",
                    catalog_path=catalog,
                    item_ids=["item-1"],
                )


if __name__ == "__main__":
    unittest.main()
