from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from runtasks.installation import (
    DAILY_CALENDAR_EXPRESSION,
    MANAGED_UNIT_NAMES,
    uninstall_user_environment,
    validate_systemd_calendar,
)
from runtasks.paths import RuntimePaths
from tests.cli_test_support import run_cli


class RunTasksInstallationCliTests(unittest.TestCase):
    def test_install_manages_services_and_cross_agent_discovery_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallationFixture(Path(directory))
            fixture.add_all_supported_agents()

            first = fixture.run("install", "--json")
            second = fixture.run("install", "--json")

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            first_payload = json.loads(first.stdout)
            self.assertEqual(first_payload["status"], "installed")
            self.assertEqual(
                {agent["name"] for agent in first_payload["agents"]},
                {"pi", "codex", "opencode", "claude"},
            )
            self.assertTrue(all(agent["discovered"] for agent in first_payload["agents"]))

            common_link = fixture.user_home / ".agents" / "skills" / "runtasks"
            claude_link = fixture.user_home / ".claude" / "skills" / "runtasks"
            self.assertTrue(common_link.is_symlink())
            self.assertTrue(claude_link.is_symlink())
            self.assertEqual(common_link.resolve(), fixture.skill_source.resolve())
            self.assertEqual(claude_link.resolve(), fixture.skill_source.resolve())

            unit_directory = fixture.user_home / ".config" / "systemd" / "user"
            scheduler_service = (unit_directory / MANAGED_UNIT_NAMES[0]).read_text(
                encoding="utf-8"
            )
            scheduler_timer = (unit_directory / MANAGED_UNIT_NAMES[1]).read_text(
                encoding="utf-8"
            )
            telegram_service = (unit_directory / MANAGED_UNIT_NAMES[2]).read_text(
                encoding="utf-8"
            )
            self.assertIn("# Managed by RunTasks", scheduler_service)
            self.assertIn("WorkingDirectory=%h/runtasks", scheduler_service)
            self.assertIn("ExecStart=%h/runtasks/bin/runtasks run-due", scheduler_service)
            self.assertIn("Type=oneshot", scheduler_service)
            self.assertIn("Environment=PATH=%h/.local/bin", scheduler_service)
            self.assertNotIn(str(fixture.user_home), scheduler_service)
            self.assertIn(
                f"OnCalendar={DAILY_CALENDAR_EXPRESSION}", scheduler_timer
            )
            self.assertIn("Persistent=true", scheduler_timer)
            self.assertIn("Unit=runtasks-scheduler.service", scheduler_timer)
            self.assertIn("ExecStart=%h/runtasks/bin/runtasks telegram listen", telegram_service)
            self.assertIn("Restart=on-failure", telegram_service)
            self.assertIn("RestartSec=5s", telegram_service)
            self.assertNotIn("run-due", telegram_service)

            commands = fixture.command_log.read_text(encoding="utf-8").splitlines()
            calendar_calls = [line for line in commands if line.startswith("systemd-analyze ")]
            self.assertEqual(
                calendar_calls,
                [
                    f"systemd-analyze calendar {DAILY_CALENDAR_EXPRESSION}",
                    f"systemd-analyze calendar {DAILY_CALENDAR_EXPRESSION}",
                ],
            )
            self.assertEqual(commands.count("systemctl --user show-environment"), 2)
            self.assertEqual(commands.count("systemctl --user daemon-reload"), 2)
            self.assertEqual(
                commands.count(
                    "systemctl --user enable --now runtasks-scheduler.timer"
                ),
                2,
            )
            self.assertEqual(
                commands.count(
                    "systemctl --user enable --now runtasks-telegram.service"
                ),
                2,
            )
            first_enable = commands.index(
                "systemctl --user enable --now runtasks-scheduler.timer"
            )
            self.assertLess(commands.index(calendar_calls[0]), first_enable)
            self.assertTrue((fixture.runtime_home / "var" / "data" / "runtasks.sqlite3").is_file())

    def test_install_reports_unsupported_systemd_without_managing_units(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallationFixture(Path(directory), add_systemd=False)

            result = fixture.run("install", "--json")

            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "error")
            self.assertIn("systemd-analyze", payload["error"])
            unit_directory = fixture.user_home / ".config" / "systemd" / "user"
            self.assertFalse(unit_directory.exists())
            self.assertFalse(
                (fixture.runtime_home / "var" / "data" / "runtasks.sqlite3").exists()
            )

    def test_unavailable_systemd_user_manager_leaves_no_partial_installation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallationFixture(Path(directory))
            fixture.environment["RUNTASKS_TEST_SYSTEMCTL_RESULT"] = "1"

            result = fixture.run("install", "--json")

            self.assertEqual(result.returncode, 2)
            self.assertIn("systemd user manager", json.loads(result.stdout)["error"])
            self.assertEqual(
                fixture.command_log.read_text(encoding="utf-8").splitlines(),
                ["systemctl --user show-environment"],
            )
            self.assertFalse(
                (fixture.runtime_home / "var" / "data" / "runtasks.sqlite3").exists()
            )
            self.assertFalse(
                (fixture.user_home / ".agents" / "skills" / "runtasks").exists()
            )
            self.assertFalse(
                (fixture.user_home / ".config" / "systemd" / "user").exists()
            )

    def test_calendar_failure_happens_before_units_are_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallationFixture(Path(directory))
            fixture.environment["RUNTASKS_TEST_CALENDAR_RESULT"] = "1"

            result = fixture.run("install", "--json")

            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            self.assertIn("calendar expression", payload["error"])
            commands = fixture.command_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                commands,
                [
                    "systemctl --user show-environment",
                    f"systemd-analyze calendar {DAILY_CALENDAR_EXPRESSION}",
                ],
            )
            self.assertFalse(
                (fixture.user_home / ".config" / "systemd" / "user").exists()
            )

    def test_agent_that_does_not_follow_symlinks_gets_a_managed_copy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallationFixture(Path(directory))
            fixture.add_agent("opencode", follows_symlinks=False)

            result = fixture.run("install", "--json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(
                payload["agents"],
                [
                    {
                        "discovered": True,
                        "fallback": "managed-copy",
                        "name": "opencode",
                    }
                ],
            )
            common_skill = fixture.user_home / ".agents" / "skills" / "runtasks"
            self.assertTrue(common_skill.is_dir())
            self.assertFalse(common_skill.is_symlink())
            self.assertTrue((common_skill / ".runtasks-managed.json").is_file())
            self.assertEqual(
                (common_skill / "SKILL.md").read_text(encoding="utf-8"),
                fixture.skill_source.joinpath("SKILL.md").read_text(encoding="utf-8"),
            )

    def test_reinstall_updates_managed_links_after_application_relocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallationFixture(Path(directory))
            installed = fixture.run("install", "--json")
            self.assertEqual(installed.returncode, 0, installed.stderr)
            relocated_home = fixture.user_home / "relocated-runtasks"
            fixture.runtime_home.rename(relocated_home)
            fixture.runtime_home = relocated_home
            fixture.skill_source = relocated_home / "skills" / "runtasks"
            fixture.environment["RUNTASKS_APPLICATION_ROOT"] = str(relocated_home)

            reinstalled = fixture.run("install", "--json")

            self.assertEqual(reinstalled.returncode, 0, reinstalled.stderr)
            self.assertEqual(
                (fixture.user_home / ".agents" / "skills" / "runtasks").resolve(),
                fixture.skill_source.resolve(),
            )
            self.assertEqual(
                (fixture.user_home / ".claude" / "skills" / "runtasks").resolve(),
                fixture.skill_source.resolve(),
            )
            scheduler = (
                fixture.user_home
                / ".config"
                / "systemd"
                / "user"
                / "runtasks-scheduler.service"
            ).read_text(encoding="utf-8")
            self.assertIn("WorkingDirectory=%h/relocated-runtasks", scheduler)

    def test_new_install_rolls_back_timer_if_telegram_activation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallationFixture(Path(directory))
            fixture.environment["RUNTASKS_TEST_SYSTEMCTL_FAIL_MATCH"] = (
                "enable --now runtasks-telegram.service"
            )

            result = fixture.run("install", "--json")

            self.assertEqual(result.returncode, 2)
            commands = fixture.command_log.read_text(encoding="utf-8").splitlines()
            self.assertIn(
                "systemctl --user disable --now runtasks-scheduler.timer", commands
            )
            self.assertIn(
                "systemctl --user disable --now runtasks-telegram.service", commands
            )
            fixture.command_log.write_text("", encoding="utf-8")

            retry = fixture.run("install", "--json")

            self.assertEqual(retry.returncode, 2)
            retry_commands = fixture.command_log.read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertIn(
                "systemctl --user disable --now runtasks-scheduler.timer",
                retry_commands,
            )

    def test_agent_probe_uses_the_isolated_temporary_home_as_its_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallationFixture(Path(directory))
            fixture.add_agent("opencode", requires_isolated_cwd=True)

            result = fixture.run("install", "--json")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["agents"][0]["discovered"])

    def test_install_preserves_an_unmanaged_skill_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallationFixture(Path(directory))
            destination = fixture.user_home / ".agents" / "skills" / "runtasks"
            destination.mkdir(parents=True)
            sentinel = destination / "keep.txt"
            sentinel.write_text("operator-owned", encoding="utf-8")

            result = fixture.run("install", "--json")

            self.assertEqual(result.returncode, 2)
            self.assertIn("not managed by RunTasks", json.loads(result.stdout)["error"])
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "operator-owned")

    def test_uninstall_removes_only_managed_files_and_preserves_runtime_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallationFixture(Path(directory))
            fixture.add_all_supported_agents()
            installed = fixture.run("install", "--json")
            self.assertEqual(installed.returncode, 0, installed.stderr)
            secret_file = fixture.runtime_home / ".env"
            secret_file.write_text("RUNTASKS_TELEGRAM_BOT_TOKEN=private\n", encoding="utf-8")
            log_file = fixture.runtime_home / "var" / "logs" / "retained.log"
            log_file.write_text("retained", encoding="utf-8")
            unknown_unit = (
                fixture.user_home
                / ".config"
                / "systemd"
                / "user"
                / "operator.service"
            )
            unknown_unit.write_text("operator-owned", encoding="utf-8")

            result = fixture.run("uninstall", "--json")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "uninstalled")
            unit_directory = fixture.user_home / ".config" / "systemd" / "user"
            for name in MANAGED_UNIT_NAMES:
                self.assertFalse((unit_directory / name).exists())
            self.assertEqual(unknown_unit.read_text(encoding="utf-8"), "operator-owned")
            self.assertFalse(
                (fixture.user_home / ".agents" / "skills" / "runtasks").exists()
            )
            self.assertFalse(
                (fixture.user_home / ".claude" / "skills" / "runtasks").exists()
            )
            self.assertTrue(secret_file.is_file())
            self.assertTrue(log_file.is_file())
            self.assertTrue(
                (fixture.runtime_home / "var" / "data" / "runtasks.sqlite3").is_file()
            )
            commands = fixture.command_log.read_text(encoding="utf-8").splitlines()
            self.assertIn(
                "systemctl --user disable --now runtasks-scheduler.timer", commands
            )
            self.assertIn(
                "systemctl --user disable --now runtasks-telegram.service", commands
            )
            self.assertIn(
                "systemctl --user stop runtasks-scheduler.service", commands
            )

    def test_uninstall_preserves_a_matching_operator_created_skill_link(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallationFixture(Path(directory))
            common_skill = fixture.user_home / ".agents" / "skills" / "runtasks"
            common_skill.parent.mkdir(parents=True)
            common_skill.symlink_to(fixture.skill_source, target_is_directory=True)

            uninstalled = fixture.run("uninstall", "--json")

            self.assertEqual(uninstalled.returncode, 0, uninstalled.stderr)
            self.assertTrue(common_skill.is_symlink())
            self.assertEqual(common_skill.resolve(), fixture.skill_source.resolve())

    def test_uninstall_preserves_operator_skill_that_replaced_a_managed_link(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallationFixture(Path(directory))
            installed = fixture.run("install", "--json")
            self.assertEqual(installed.returncode, 0, installed.stderr)
            common_skill = fixture.user_home / ".agents" / "skills" / "runtasks"
            common_skill.unlink()
            common_skill.mkdir()
            operator_file = common_skill / "operator-owned.txt"
            operator_file.write_text("keep", encoding="utf-8")

            result = fixture.run("uninstall", "--json")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(operator_file.read_text(encoding="utf-8"), "keep")

    def test_uninstall_failure_preserves_managed_unit_definitions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallationFixture(Path(directory))
            installed = fixture.run("install", "--json")
            self.assertEqual(installed.returncode, 0, installed.stderr)
            fixture.environment["RUNTASKS_TEST_SYSTEMCTL_RESULT"] = "1"

            result = fixture.run("uninstall", "--json")

            self.assertEqual(result.returncode, 2)
            self.assertIn("could not be disabled", json.loads(result.stdout)["error"])
            unit_directory = fixture.user_home / ".config" / "systemd" / "user"
            for name in MANAGED_UNIT_NAMES:
                self.assertTrue((unit_directory / name).is_file())

    def test_uninstall_does_not_stop_or_remove_unmanaged_same_name_units(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallationFixture(Path(directory))
            unit_directory = fixture.user_home / ".config" / "systemd" / "user"
            unit_directory.mkdir(parents=True)
            unmanaged = unit_directory / "runtasks-telegram.service"
            unmanaged.write_text("[Service]\nExecStart=/operator/command\n", encoding="utf-8")

            result = fixture.run("uninstall", "--json")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                unmanaged.read_text(encoding="utf-8"),
                "[Service]\nExecStart=/operator/command\n",
            )
            commands = fixture.command_log.read_text(encoding="utf-8").splitlines()
            self.assertNotIn(
                "systemctl --user disable --now runtasks-telegram.service", commands
            )

    def test_uninstall_removes_runtime_data_only_with_explicit_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallationFixture(Path(directory))
            installed = fixture.run("install", "--json")
            self.assertEqual(installed.returncode, 0, installed.stderr)
            (fixture.runtime_home / ".env").write_text("private", encoding="utf-8")

            result = fixture.run("uninstall", "--remove-data", "--json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["data_removed"])
            self.assertFalse((fixture.runtime_home / ".env").exists())
            self.assertFalse((fixture.runtime_home / "config").exists())
            self.assertFalse((fixture.runtime_home / "var").exists())
            self.assertTrue(fixture.skill_source.is_dir())


class RuntimeDataRemovalTests(unittest.TestCase):
    def test_explicit_data_removal_preserves_account_global_telegram_locks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            user_home = Path(directory) / "user"
            runtime_home = user_home / "runtasks"
            data_directory = runtime_home / "var" / "data"
            data_directory.mkdir(parents=True)
            database = data_directory / "runtasks.sqlite3"
            database.write_text("database", encoding="utf-8")
            lock_file = data_directory / "telegram-poller-token.lock"
            lock_file.write_text("active-lock-inode", encoding="utf-8")
            runtime_paths = RuntimePaths(
                home=runtime_home,
                global_lock_directory=data_directory,
            )

            outcome = uninstall_user_environment(
                runtime_paths,
                remove_data=True,
                environment={"HOME": str(user_home), "PATH": ""},
            )

            self.assertTrue(outcome.data_removed)
            self.assertFalse(database.exists())
            self.assertEqual(
                lock_file.read_text(encoding="utf-8"), "active-lock-inode"
            )


@unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze is unavailable")
class SystemdCalendarIntegrationTests(unittest.TestCase):
    def test_daily_calendar_expression_is_accepted_by_installed_systemd(self) -> None:
        validate_systemd_calendar(shutil.which("systemd-analyze") or "systemd-analyze")


class InstallationFixture:
    def __init__(self, root: Path, *, add_systemd: bool = True) -> None:
        self.user_home = root / "user"
        self.runtime_home = self.user_home / "runtasks"
        self.skill_source = self.runtime_home / "skills" / "runtasks"
        self.skill_source.mkdir(parents=True)
        self.skill_source.joinpath("SKILL.md").write_text(
            "---\nname: runtasks\ndescription: Test RunTasks skill.\n---\n",
            encoding="utf-8",
        )
        bin_directory = self.runtime_home / "bin"
        bin_directory.mkdir(parents=True)
        bin_directory.joinpath("runtasks").write_text("#!/bin/sh\n", encoding="utf-8")
        self.fake_bin = self.user_home / ".local" / "bin"
        self.fake_bin.mkdir(parents=True)
        uv = shutil.which("uv")
        if uv is None:
            raise RuntimeError("uv is required for tests")
        self.fake_bin.joinpath("uv").symlink_to(uv)
        self.fake_bin.joinpath("sh").symlink_to("/bin/sh")
        self.command_log = root / "commands.log"
        self.command_log.write_text("", encoding="utf-8")
        self.environment = {
            "HOME": str(self.user_home),
            "PATH": str(self.fake_bin),
            "XDG_CONFIG_HOME": str(self.user_home / ".config"),
            "RUNTASKS_APPLICATION_ROOT": str(self.runtime_home),
            "RUNTASKS_TEST_COMMAND_LOG": str(self.command_log),
        }
        if add_systemd:
            self._add_systemd_fakes()

    def run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return run_cli(
            self.runtime_home,
            *arguments,
            extra_environment=self.environment,
        )

    def add_all_supported_agents(self) -> None:
        for name in ("pi", "codex", "opencode", "claude"):
            self.add_agent(name)

    def add_agent(
        self,
        name: str,
        *,
        follows_symlinks: bool = True,
        requires_isolated_cwd: bool = False,
    ) -> None:
        if name == "pi":
            body = """
read request
printf '%s\n' '{"type":"response","command":"get_commands","success":true,"data":{"commands":[{"name":"skill:runtasks","source":"skill"}]}}'
"""
        elif name == "codex":
            body = """
printf '%s\n' '[{"type":"message","content":[{"type":"input_text","text":"Available skills: runtasks"}]}]'
"""
        elif name == "opencode":
            conditions = []
            if not follows_symlinks:
                conditions.append('[ -L "$HOME/.agents/skills/runtasks" ]')
            if requires_isolated_cwd:
                conditions.append('[ "$PWD" != "$HOME" ]')
            condition = " || ".join(conditions) if conditions else "false"
            body = f"""
if {condition}; then
  printf '%s\\n' '[]'
else
  printf '%s\\n' '[{{"name":"runtasks"}}]'
fi
"""
        elif name == "claude":
            body = """
debug_file=
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--debug-file" ]; then
    shift
    debug_file=$1
  fi
  shift
done
if [ -n "$debug_file" ]; then
  printf '%s\n' 'Loaded 1 unique skills (1 unconditional, 0 conditional, managed: 0, user: 1, project: 0)' > "$debug_file"
fi
printf '%s\n' 'Not logged in'
exit 1
"""
        else:
            raise ValueError(name)
        self._write_executable(name, body)

    def _add_systemd_fakes(self) -> None:
        self._write_executable(
            "systemd-analyze",
            """
printf '%s\n' "systemd-analyze $*" >> "$RUNTASKS_TEST_COMMAND_LOG"
exit "${RUNTASKS_TEST_CALENDAR_RESULT:-0}"
""",
        )
        self._write_executable(
            "systemctl",
            """
printf '%s\n' "systemctl $*" >> "$RUNTASKS_TEST_COMMAND_LOG"
if [ -n "${RUNTASKS_TEST_SYSTEMCTL_FAIL_MATCH:-}" ] &&
   case "$*" in *"$RUNTASKS_TEST_SYSTEMCTL_FAIL_MATCH"*) true;; *) false;; esac; then
  exit 1
fi
exit "${RUNTASKS_TEST_SYSTEMCTL_RESULT:-0}"
""",
        )

    def _write_executable(self, name: str, body: str) -> None:
        path = self.fake_bin / name
        path.write_text(f"#!/bin/sh\nset -eu\n{body}", encoding="utf-8")
        path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
