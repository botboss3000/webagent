from __future__ import annotations

import asyncio
import importlib.util
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "abilities"
    / "Administrator"
    / "code_index"
    / "code_index.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("test_code_index_module", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def _temp_index(module):
    with tempfile.TemporaryDirectory() as temp_dir:
        project_root = Path(temp_dir) / "project"
        project_root.mkdir()
        db_path = project_root / "data" / "db" / "index.db"
        with (
            patch.object(module, "_project_root", return_value=project_root),
            patch.object(module, "_index_db_path", return_value=db_path),
            patch.object(
                module,
                "_legacy_index_db_path",
                return_value=project_root / "legacy" / "index.db",
            ),
        ):
            yield project_root


class CodeIndexCommentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module()

    def test_comment_audit_accepts_header_paths_and_markers(self):
        with _temp_index(self.module) as root:
            (root / "ui").mkdir()
            (root / "app" / "api").mkdir(parents=True)
            (root / "ui" / "widget.html").write_text("", encoding="utf-8")
            (root / "app" / "api" / "widget.py").write_text("", encoding="utf-8")
            source = """'use strict';
/**
 * Widget controller — coordinates the widget surface and its API.
 * Breadcrumbs: ui/widget.html and app/api/widget.py.
 */
// KEEP (intentional): public compatibility entry point.
"""

            result = self.module._analyze_comment_standard("ui/widget.js", source)

            self.assertEqual(result["status"], "compliant")
            self.assertEqual(result["header_style"], "jsdoc")
            self.assertTrue(result["purpose_header"])
            self.assertEqual(
                [item["target_path"] for item in result["breadcrumbs"]],
                ["ui/widget.html", "app/api/widget.py"],
            )
            self.assertTrue(all(item["target_exists"] for item in result["breadcrumbs"]))
            self.assertEqual(result["markers"][0]["marker"], "KEEP (INTENTIONAL)")

    def test_comment_audit_reports_missing_stale_and_exempt(self):
        with _temp_index(self.module):
            missing = self.module._analyze_comment_standard(
                "ui/widget.js", "export function widget() {}\n"
            )
            stale = self.module._analyze_comment_standard(
                "ui/widget.css",
                "/* Widget styles — styles the widget.\n"
                "   Breadcrumb: ui/missing-widget.html. */\n",
            )
            exempt = self.module._analyze_comment_standard(
                "ui/background/aurora/aurora.js", "/* ===== AURORA ===== */\n"
            )
            json_file = self.module._analyze_comment_standard(
                "ui/manifest.json", '{"name": "WebAgent"}'
            )

            self.assertEqual(missing["status"], "missing")
            self.assertEqual(stale["status"], "stale")
            self.assertEqual(exempt["status"], "exempt")
            self.assertEqual(json_file["status"], "not_applicable")

    def test_store_and_lookup_expose_comment_compliance(self):
        with _temp_index(self.module) as root:
            (root / "ui").mkdir()
            (root / "ui" / "widget.html").write_text("", encoding="utf-8")
            source = (
                "// Widget controller — coordinates the widget surface.\n"
                "// Breadcrumb: ui/widget.html.\n"
                "// REMOVE-WHEN: the widget surface is removed.\n"
            )

            stored = json.loads(asyncio.run(self.module._index_store(
                action="file",
                file_path="ui/widget.js",
                source=source,
                summary="Widget controller",
            )))
            comments = json.loads(asyncio.run(self.module._index_lookup(
                action="comments",
                comment_status="compliant",
            )))
            styles = json.loads(asyncio.run(self.module._index_lookup(
                action="comments",
                header_style="line",
            )))
            marker = json.loads(asyncio.run(self.module._index_lookup(
                action="comments",
                marker="REMOVE-WHEN",
            )))
            file_result = json.loads(asyncio.run(self.module._index_lookup(
                action="file",
                file_path="ui/widget.js",
            )))
            summary = json.loads(asyncio.run(
                self.module._index_lookup(action="summary")
            ))

            self.assertEqual(stored["comment_status"], "compliant")
            self.assertEqual(stored["breadcrumbs_checked"], 1)
            self.assertEqual(stored["size_bytes"], len(source.encode("utf-8")))
            self.assertEqual(stored["line_count"], 3)
            self.assertEqual(comments["count"], 1)
            self.assertEqual(styles["count"], 1)
            self.assertEqual(marker["results"][0]["marker"], "REMOVE-WHEN")
            self.assertEqual(
                file_result["file"]["comment_audit"]["status"], "compliant"
            )
            self.assertEqual(
                file_result["file"]["breadcrumbs"][0]["target_exists"], 1
            )
            self.assertEqual(
                file_result["file"]["size"], len(source.encode("utf-8"))
            )
            self.assertEqual(file_result["file"]["line_count"], 3)
            self.assertEqual(summary["comment_compliance"], {"compliant": 1})
            self.assertEqual(summary["comment_header_styles"], {"line": 1})
            self.assertEqual(summary["total_size_bytes"], len(source.encode("utf-8")))
            self.assertEqual(summary["total_lines"], 3)

    def test_existing_database_gets_line_count_column(self):
        with _temp_index(self.module):
            self.module._index_db_path().parent.mkdir(parents=True, exist_ok=True)
            connection = self.module.sqlite3.connect(str(self.module._index_db_path()))
            connection.execute(
                "CREATE TABLE indexed_files ("
                "path TEXT PRIMARY KEY, language TEXT NOT NULL, sha256 TEXT NOT NULL, "
                "size INTEGER NOT NULL, summary TEXT, last_indexed TIMESTAMP NOT NULL)"
            )
            connection.commit()
            connection.close()

            migrated = self.module._ensure_schema()
            columns = {
                row["name"]
                for row in migrated.execute("PRAGMA table_info(indexed_files)").fetchall()
            }
            migrated.close()

            self.assertIn("line_count", columns)

    def test_index_path_is_repo_relative(self):
        self.assertEqual(self.module._project_root().name, "webagent-dev")
        self.assertEqual(
            self.module._index_db_path(),
            self.module._project_root() / "data" / "db" / "index.db",
        )


if __name__ == "__main__":
    unittest.main()
