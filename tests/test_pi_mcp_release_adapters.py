from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from runtasks.pi_mcp_release_adapters import (
    ChangelogReleaseSource,
    GitHubReleaseSource,
    HttpResponse,
    NpmPackageRegistry,
    PiImportanceEvaluator,
    PiInstallationMetadataSource,
    ProcessResult,
)
from runtasks.pi_mcp_releases import (
    EvaluationEvidence,
    NormalizedRelease,
    NormalizedSourceEvidence,
    ReleaseCheckError,
)


class FakeProcessRunner:
    def __init__(self, results: list[ProcessResult]) -> None:
        self.results = results
        self.requests: list[tuple[tuple[str, ...], float, Path | None]] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        cwd: Path | None = None,
    ) -> ProcessResult:
        self.requests.append((argv, timeout_seconds, cwd))
        return self.results.pop(0)


class FakeHttpClient:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def get(self, url: str, *, timeout_seconds: float) -> HttpResponse:
        self.urls.append(url)
        return self.responses.pop(0)


class TimeoutProcessRunner:
    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        cwd: Path | None = None,
    ) -> ProcessResult:
        del argv, timeout_seconds, cwd
        raise ReleaseCheckError("process timed out")


class PiMcpReleaseAdapterTests(unittest.TestCase):
    def test_installed_version_comes_from_current_agent_directory_package_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent_dir = Path(directory) / "custom-agent"
            package_dir = agent_dir / "npm" / "node_modules" / "pi-mcp-adapter"
            package_dir.mkdir(parents=True)
            (package_dir / "package.json").write_text(
                json.dumps(
                    {
                        "name": "pi-mcp-adapter",
                        "version": "9.8.7",
                        "peerDependencies": {
                            "@earendil-works/pi-ai": "^0.90.0"
                        },
                    }
                ),
                encoding="utf-8",
            )
            runner = FakeProcessRunner([ProcessResult(0, "0.90.1\n", "")])

            evidence = PiInstallationMetadataSource(
                agent_dir=agent_dir,
                process_runner=runner,
            ).detect()

            self.assertEqual(evidence.adapter_version, "9.8.7")
            self.assertEqual(evidence.pi_version, "0.90.1")
            self.assertEqual(evidence.adapter_pi_requirement, "^0.90.0")
            self.assertEqual(runner.requests[0][0], ("pi", "--version"))

    def test_registry_uses_the_configured_read_only_npm_command_and_latest_dist_tag(self) -> None:
        runner = FakeProcessRunner(
            [
                ProcessResult(
                    0,
                    json.dumps(
                        {
                            "dist-tags": {"latest": "2.27.0", "next": "3.0.0-beta.1"},
                            "versions": ["2.26.1", "2.27.0", "3.0.0-beta.1"],
                            "peerDependencies": {
                                "@earendil-works/pi-ai": "^0.84.1"
                            },
                        }
                    ),
                    "",
                )
            ]
        )

        snapshot = NpmPackageRegistry(
            process_runner=runner,
            npm_command=("mise", "exec", "node@24", "--", "npm"),
        ).lookup()

        self.assertEqual(snapshot.latest_version, "2.27.0")
        self.assertEqual(snapshot.stable_versions, ("2.26.1", "2.27.0"))
        self.assertEqual(snapshot.available_pi_requirement, "^0.84.1")
        self.assertEqual(
            runner.requests[0][0],
            (
                "mise",
                "exec",
                "node@24",
                "--",
                "npm",
                "view",
                "pi-mcp-adapter",
                "dist-tags",
                "versions",
                "peerDependencies",
                "--json",
            ),
        )

    def test_github_releases_and_changelog_normalize_requested_versions(self) -> None:
        github = FakeHttpClient(
            [
                HttpResponse(
                    200,
                    json.dumps(
                        [
                            {
                                "tag_name": "v2.27.0",
                                "name": "Adapter 2.27.0",
                                "body": "OAuth callback fix.",
                                "html_url": "https://github.com/example/releases/v2.27.0",
                                "draft": False,
                                "prerelease": False,
                            },
                            {
                                "tag_name": "v2.26.1",
                                "name": "Adapter 2.26.1",
                                "body": "Approval gate fix.",
                                "html_url": "https://github.com/example/releases/v2.26.1",
                                "draft": False,
                                "prerelease": False,
                            },
                        ]
                    ),
                )
            ]
        )
        changelog = FakeHttpClient(
            [
                HttpResponse(
                    200,
                    """# Changelog

## [Unreleased]

## [2.27.0] - 2026-08-20
### Fixed
- OAuth callback fix.

## [2.26.1] - 2026-08-18
### Fixed
- Approval gate fix.
""",
                )
            ]
        )
        versions = ("2.26.1", "2.27.0")

        release_notes = GitHubReleaseSource(http_client=github).collect(versions)
        changelog_notes = ChangelogReleaseSource(http_client=changelog).collect(
            versions
        )

        self.assertEqual(tuple(release_notes), versions)
        self.assertEqual(tuple(changelog_notes), versions)
        self.assertIn("OAuth callback fix", release_notes["2.27.0"].body)
        self.assertIn("Approval gate fix", changelog_notes["2.26.1"].body)

    def test_pi_evaluator_uses_no_tools_and_rejects_malformed_or_timed_out_output(self) -> None:
        evidence = EvaluationEvidence(
            installed_version="2.26.1",
            available_version="2.27.0",
            pi_version="0.84.2",
            adapter_pi_requirement="^0.84.1",
            importance_context={"active_mcp_servers": ["stripe"]},
            releases=(
                NormalizedRelease(
                    version="2.27.0",
                    sources=(
                        NormalizedSourceEvidence(
                            source="changelog",
                            title="2.27.0",
                            body="OAuth callback safety fix.",
                            reference="https://example.invalid/changelog#2270",
                        ),
                    ),
                ),
            ),
            available_pi_requirement="^0.84.1",
        )
        valid_runner = FakeProcessRunner(
            [
                ProcessResult(
                    0,
                    json.dumps(
                        {
                            "importance": "important",
                            "category": "credential-oauth",
                            "reason": "OAuth safety affects the installation.",
                            "recommendation": "Update after approval.",
                            "confidence": "high",
                        }
                    ),
                    "",
                )
            ]
        )

        assessment = PiImportanceEvaluator(process_runner=valid_runner).evaluate(
            evidence
        )

        self.assertEqual(assessment.category, "credential-oauth")
        argv = valid_runner.requests[0][0]
        self.assertIn("--no-tools", argv)
        self.assertIn("--no-context-files", argv)
        for rule in (
            "Security fix affecting the installation",
            "Credential-handling or OAuth safety fix",
            "Compatibility fix required by the installed Pi version",
            "currently broken active MCP server",
            "Protocol negotiation or connection fix",
            "Approval-gate or output-guard safety fix",
            "Serious operational defect",
        ):
            self.assertIn(rule, argv[-1])
        self.assertIn("2.27.0", argv[-1])

        malformed = PiImportanceEvaluator(
            process_runner=FakeProcessRunner(
                [ProcessResult(0, "```json\\n{}\\n```", "")]
            )
        )
        with self.assertRaises(ReleaseCheckError):
            malformed.evaluate(evidence)
        with self.assertRaises(ReleaseCheckError):
            PiImportanceEvaluator(
                process_runner=TimeoutProcessRunner()
            ).evaluate(evidence)


if __name__ == "__main__":
    unittest.main()
