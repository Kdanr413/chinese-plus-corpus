from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from corpus_registry import config as registry_config
from corpus_registry import (
    CorpusRegistry,
    DatabaseConfigurationError,
    DatabaseSettings,
    corpus_data_root,
)
from corpus_registry.db import connect
from corpus_registry.migrations import initialize_database
from corpus_registry.repository import inspect_release_files


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CorpusRegistryStaticTests(unittest.TestCase):
    def test_schema_contains_required_tables_and_relationships(self) -> None:
        sql = (PROJECT_ROOT / "db/migrations/0001_corpus_registry.sql").read_text(encoding="utf-8")
        for table in ("corpora", "corpus_versions", "corpus_files", "processing_jobs", "query_logs"):
            self.assertIn(f"CREATE TABLE {table}", sql)
        self.assertIn("REFERENCES corpora(id)", sql)
        self.assertIn("REFERENCES corpus_versions(id)", sql)
        self.assertIn("default_version_id", sql)
        self.assertIn("DEFERRABLE INITIALLY DEFERRED", sql)

    def test_database_settings_use_five_environment_variables(self) -> None:
        with self.assertRaises(DatabaseConfigurationError):
            DatabaseSettings.from_environment({})
        settings = DatabaseSettings.from_environment(
            {
                "DB_HOST": "127.0.0.1",
                "DB_PORT": "5432",
                "DB_NAME": "corpus_registry",
                "DB_USER": "corpus_app",
                "DB_PASSWORD": "local-secret",
            }
        )
        self.assertEqual(settings.connection_kwargs()["dbname"], "corpus_registry")
        self.assertNotIn("local-secret", repr(settings))

    def test_local_dotenv_is_loaded_and_process_environment_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            data_root = Path(directory) / "data"
            env_file.write_text(
                "DB_HOST=from-file\n"
                "DB_PORT=5432\n"
                "DB_NAME=corpus_platform\n"
                "DB_USER=corpus_user\n"
                "DB_PASSWORD=local-secret\n"
                f"CORPUS_DATA_ROOT={data_root}\n",
                encoding="utf-8",
            )
            with (
                mock.patch.object(registry_config, "PROJECT_ENV_FILE", env_file),
                mock.patch.dict(os.environ, {"DB_HOST": "127.0.0.1"}, clear=True),
            ):
                settings = DatabaseSettings.from_environment()
                resolved_root = corpus_data_root()
        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.name, "corpus_platform")
        self.assertEqual(settings.user, "corpus_user")
        self.assertEqual(resolved_root, data_root.resolve())

    def test_corpus_data_root_is_required_and_configurable(self) -> None:
        with self.assertRaises(DatabaseConfigurationError):
            corpus_data_root(environment={})
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(corpus_data_root(environment={"CORPUS_DATA_ROOT": directory}), Path(directory).resolve())

    def test_relative_corpus_data_root_is_resolved_from_project(self) -> None:
        expected = (registry_config.PROJECT_ROOT / "data").resolve()
        self.assertEqual(
            corpus_data_root(environment={"CORPUS_DATA_ROOT": "data"}),
            expected,
        )

    def test_example_configuration_contains_no_password(self) -> None:
        values = {}
        for line in (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values[key] = value
        self.assertEqual(values["DB_PASSWORD"], "")
        self.assertEqual(values["CORPUS_DATA_ROOT"], "data")
        self.assertEqual(values["DB_NAME"], "corpus_platform")
        self.assertEqual(values["DB_USER"], "corpus_user")
        self.assertNotIn("CORPUS_DATABASE_URL", values)

    def test_legacy_release_files_can_be_inspected_without_modification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            release = Path(directory)
            source = release / "corpus_index.txt"
            source.write_text("Doc 示例\nItem: 新能源。\n", encoding="gb18030")
            before = source.read_bytes()
            files, checksum = inspect_release_files(release)
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0]["relative_path"], "corpus_index.txt")
            self.assertEqual(files[0]["role"], "corpus")
            self.assertEqual(len(checksum), 64)
            self.assertEqual(source.read_bytes(), before)

    def test_gb18030_detection_tolerates_sample_ending_inside_character(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            release = Path(directory)
            source = release / "corpus_index.txt"
            source.write_bytes(b"A" * 65535 + "中".encode("gb18030") + b"\n")
            files, _ = inspect_release_files(release)
            self.assertEqual(files[0]["encoding"], "gb18030")


def _postgresql_is_configured() -> bool:
    try:
        DatabaseSettings.from_environment()
    except DatabaseConfigurationError:
        return False
    return True


@unittest.skipUnless(
    _postgresql_is_configured(),
    "set DB_NAME, DB_USER and DB_PASSWORD to run PostgreSQL CRUD integration tests",
)
class CorpusRegistryPostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.settings = DatabaseSettings.from_environment()
        initialize_database(cls.settings)

    def test_crud_release_files_jobs_and_query_log(self) -> None:
        corpus_key = f"test-{uuid.uuid4().hex[:10]}"
        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory)
            registry = CorpusRegistry(self.settings, data_root=data_root)
            release = data_root / "releases" / corpus_key / "1.0.0"
            release.mkdir(parents=True)
            (release / "corpus_index.txt").write_text(
                "Doc 示例\nItem: 动力电池安全。\n", encoding="gb18030"
            )
            try:
                corpus = registry.create_corpus(corpus_key, "测试语料库")
                self.assertEqual(registry.get_corpus(corpus_key)["id"], corpus["id"])
                self.assertIn(
                    corpus["id"],
                    {item["id"] for item in registry.list_corpora()},
                )
                updated = registry.update_corpus(
                    corpus_key, name="测试语料库（更新）",
                    description="CRUD integration test", status="active"
                )
                self.assertEqual(updated["name"], "测试语料库（更新）")
                version = registry.register_release(
                    corpus_key=corpus_key, version="1.0.0", release_path=release,
                    corpus_format_version="bcc-doc-global-group-item/1.0",
                    pipeline_version="1.0", search_engine_version="LangSC.BCC 2.0.7",
                    status="ready", searchable=True, make_default=True,
                )
                self.assertEqual(registry.get_version(corpus_key)["id"], version["id"])
                self.assertEqual(
                    registry.list_versions(corpus["id"])[0]["id"], version["id"]
                )
                files = registry.list_files(corpus_key, "1.0.0")
                self.assertEqual(files[0]["relative_path"], "corpus_index.txt")
                self.assertEqual(
                    registry.resolve_release_path(corpus_key, "1.0.0"),
                    release.resolve(),
                )
                self.assertTrue(registry.verify_registered_files(corpus_key, "1.0.0")["valid"])
                job = registry.create_processing_job(
                    corpus_key, release.parent / "workspace", "reprocess", parent_version="1.0.0"
                )
                self.assertEqual(registry.update_processing_job(job["id"], "succeeded")["status"], "succeeded")
                log = registry.log_query(
                    corpus_key, "1.0.0", "新能源", "Freq", result_count=1, duration_ms=3
                )
                self.assertEqual(log["result_count"], 1)
            finally:
                with connect(self.settings) as connection, connection.transaction():
                    found = connection.execute(
                        "SELECT id FROM corpora WHERE corpus_key=%s", (corpus_key,)
                    ).fetchone()
                    if found:
                        connection.execute("UPDATE corpora SET default_version_id=NULL WHERE id=%s", (found["id"],))
                        connection.execute("DELETE FROM query_logs WHERE corpus_id=%s", (found["id"],))
                        connection.execute("DELETE FROM processing_jobs WHERE corpus_id=%s", (found["id"],))
                        connection.execute("DELETE FROM corpus_files WHERE corpus_version_id IN (SELECT id FROM corpus_versions WHERE corpus_id=%s)", (found["id"],))
                        connection.execute("DELETE FROM corpus_versions WHERE corpus_id=%s", (found["id"],))
                        connection.execute("DELETE FROM corpora WHERE id=%s", (found["id"],))
            with connect(self.settings) as connection:
                remaining = connection.execute(
                    "SELECT count(*) AS count FROM corpora WHERE corpus_key=%s",
                    (corpus_key,),
                ).fetchone()["count"]
            self.assertEqual(remaining, 0)

    def test_unique_constraint_rolls_back_the_whole_transaction(self) -> None:
        from psycopg.errors import UniqueViolation

        corpus_key = f"rollback-{uuid.uuid4().hex[:10]}"
        with connect(self.settings) as connection:
            with self.assertRaises(UniqueViolation):
                with connection.transaction():
                    connection.execute(
                        "INSERT INTO corpora(id, corpus_key, name) VALUES (%s, %s, %s)",
                        (uuid.uuid4(), corpus_key, "回滚测试一"),
                    )
                    connection.execute(
                        "INSERT INTO corpora(id, corpus_key, name) VALUES (%s, %s, %s)",
                        (uuid.uuid4(), corpus_key, "回滚测试二"),
                    )
            remaining = connection.execute(
                "SELECT count(*) AS count FROM corpora WHERE corpus_key=%s",
                (corpus_key,),
            ).fetchone()["count"]
        self.assertEqual(remaining, 0)

    def test_list_versions_rejects_unknown_corpus_uuid(self) -> None:
        from corpus_registry import RegistryNotFoundError

        registry = CorpusRegistry(self.settings)
        with self.assertRaises(RegistryNotFoundError):
            registry.list_versions(uuid.uuid4())


if __name__ == "__main__":
    unittest.main()
