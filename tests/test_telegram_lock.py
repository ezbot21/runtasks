from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from runtasks.paths import RuntimePaths
from runtasks.telegram import PollerAlreadyRunningError, PollerGuard


TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"


class PollerGuardTests(unittest.TestCase):
    def test_only_one_process_can_poll_the_same_bot_across_runtime_homes(self) -> None:
        script = """
from pathlib import Path
import sys
from runtasks.telegram import PollerGuard
with PollerGuard(Path(sys.argv[1])):
    print('ready', flush=True)
    sys.stdin.readline()
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            account_home = root / "account-home"
            with patch(
                "runtasks.paths._canonical_user_home",
                return_value=account_home,
            ):
                first_paths = RuntimePaths.from_environment(
                    {
                        "HOME": str(root / "first-environment-home"),
                        "RUNTASKS_HOME": str(root / "first-runtime"),
                    }
                )
                second_paths = RuntimePaths.from_environment(
                    {
                        "HOME": str(root / "second-environment-home"),
                        "RUNTASKS_HOME": str(root / "second-runtime"),
                    }
                )
            first_lock = first_paths.telegram_poller_lock_file(TOKEN)
            second_lock = second_paths.telegram_poller_lock_file(TOKEN)
            self.assertEqual(first_lock, second_lock)
            self.assertTrue(
                str(first_lock).startswith(str(account_home / "runtasks"))
            )

            holder = subprocess.Popen(
                [sys.executable, "-c", script, str(first_lock)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                if holder.stdout is None:
                    self.fail("guard holder stdout was not captured")
                self.assertEqual(holder.stdout.readline().strip(), "ready")

                with self.assertRaises(PollerAlreadyRunningError):
                    with PollerGuard(second_lock):
                        self.fail("the second poller guard must not be acquired")
            finally:
                if holder.stdin is not None:
                    holder.stdin.write("stop\n")
                    holder.stdin.flush()
                _, stderr = holder.communicate(timeout=5)
                self.assertEqual(holder.returncode, 0, stderr)

            with PollerGuard(second_lock):
                pass

            self.assertTrue(first_lock.is_file())

    def test_different_bot_tokens_use_different_global_locks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = RuntimePaths.from_environment(
                {"HOME": str(Path(directory) / "environment-home")},
                global_lock_directory=Path(directory) / "global-locks",
            )
            first = paths.telegram_poller_lock_file(TOKEN)
            second = paths.telegram_poller_lock_file(f"{TOKEN}-different")

            self.assertNotEqual(first, second)
            with PollerGuard(first), PollerGuard(second):
                pass


if __name__ == "__main__":
    unittest.main()
