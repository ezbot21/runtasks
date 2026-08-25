from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from runtasks.release_checks import scan_repository
from tests.cli_test_support import PROJECT_ROOT


class ReleaseCheckTests(unittest.TestCase):
    def test_secret_scan_rejects_credentials_private_env_and_numeric_operator_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src" / "leak.py").write_text(
                "token = '" + "ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890'\n",
                encoding="utf-8",
            )
            (root / ".env").write_text(
                "RUNTASKS_TELEGRAM_BOT_TOKEN="
                + "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi\n"  # release-check: allow-fake-secret
                + "RUNTASKS_TELEGRAM_ALLOWED_USER_IDS=998877665\n",
                encoding="utf-8",
            )

            findings = scan_repository(
                root,
                tracked_paths=(Path("src/leak.py"), Path(".env")),
            )

            kinds = {finding.kind for finding in findings}
            self.assertIn("secret", kinds)
            self.assertIn("private-environment", kinds)
            self.assertIn("production-id", kinds)

    def test_portability_scan_rejects_machine_paths_runtime_state_and_absolute_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "var" / "data").mkdir(parents=True)
            (root / "src" / "__pycache__").mkdir()
            (root / "src" / "machine.py").write_text(
                "ROOT = '/" + "home/example/runtasks'\n",
                encoding="utf-8",
            )
            (root / "var" / "data" / "runtasks.sqlite3").write_bytes(b"sqlite")
            (root / "src" / "__pycache__" / "machine.pyc").write_bytes(b"cache")
            link = root / "src" / "local-link"
            try:
                link.symlink_to("/opt/private/tool")
            except OSError:
                self.skipTest("symlinks are unavailable")

            findings = scan_repository(
                root,
                tracked_paths=(
                    Path("src/machine.py"),
                    Path("var/data/runtasks.sqlite3"),
                    Path("src/__pycache__/machine.pyc"),
                    Path("src/local-link"),
                ),
            )

            kinds = {finding.kind for finding in findings}
            self.assertIn("hardcoded-home", kinds)
            self.assertIn("runtime-state", kinds)
            self.assertIn("machine-dependency", kinds)
            self.assertIn(
                ("machine-dependency", "src/__pycache__/machine.pyc"),
                {(finding.kind, finding.path) for finding in findings},
            )

    def test_release_boundary_rejects_web_openclaw_and_arbitrary_shell_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "pyproject.toml").write_text(
                '[project]\ndependencies = ["openclaw", "fastapi"]\n',
                encoding="utf-8",
            )
            (root / "src" / "unsafe.py").write_text(
                "import os\nos.system('policy text')\n",
                encoding="utf-8",
            )

            findings = scan_repository(
                root,
                tracked_paths=(Path("pyproject.toml"), Path("src/unsafe.py")),
            )

            kinds = {finding.kind for finding in findings}
            self.assertIn("forbidden-dependency", kinds)
            self.assertIn("arbitrary-shell", kinds)

    def test_public_repository_passes_secret_and_portability_checks(self) -> None:
        findings = scan_repository(PROJECT_ROOT)
        self.assertEqual(findings, ())

    def test_public_runtime_has_fts5_and_validates_the_systemd_calendar_when_supported(self) -> None:
        with sqlite3.connect(":memory:") as connection:
            connection.execute("CREATE VIRTUAL TABLE release_fts USING fts5(content)")
            connection.execute("INSERT INTO release_fts(content) VALUES ('ready')")
            self.assertEqual(
                connection.execute(
                    "SELECT content FROM release_fts WHERE release_fts MATCH 'ready'"
                ).fetchone(),
                ("ready",),
            )

        systemd_analyze = shutil.which("systemd-analyze")
        if systemd_analyze is None:
            self.skipTest("systemd-analyze is not installed")
        result = subprocess.run(
            [systemd_analyze, "calendar", "*-*-* 09:00:00 Asia/Singapore"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Normalized form", result.stdout)

    def test_release_check_module_reports_machine_readable_findings(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "runtasks.release_checks",
                "--json",
            ],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"findings": [], "status": "ok"})


if __name__ == "__main__":
    unittest.main()
