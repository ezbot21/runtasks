from __future__ import annotations

from dataclasses import dataclass
import unittest

from runtasks.pi_mcp_releases import (
    EvaluationEvidence,
    ImportanceAssessment,
    InstallationEvidence,
    RegistrySnapshot,
    ReleaseSourceNote,
    PiMcpReleaseChecker,
    ReleaseCheckError,
)


@dataclass
class FakeInstalledVersionSource:
    evidence: InstallationEvidence

    def detect(self) -> InstallationEvidence:
        return self.evidence


@dataclass
class FakeRegistry:
    snapshot: RegistrySnapshot

    def lookup(self) -> RegistrySnapshot:
        return self.snapshot


class UnexpectedReleaseSource:
    name = "unexpected"
    reference = "https://example.invalid/releases"

    def collect(self, versions: tuple[str, ...]) -> dict[str, ReleaseSourceNote]:
        raise AssertionError(f"release source should not be called for {versions}")


class UnexpectedEvaluator:
    def evaluate(self, evidence: EvaluationEvidence) -> ImportanceAssessment:
        raise AssertionError(f"evaluator should not be called for {evidence}")


@dataclass
class FakeReleaseSource:
    name: str
    reference: str
    notes: dict[str, ReleaseSourceNote]
    requested_versions: tuple[str, ...] | None = None

    def collect(self, versions: tuple[str, ...]) -> dict[str, ReleaseSourceNote]:
        self.requested_versions = versions
        return self.notes


@dataclass
class RecordingEvaluator:
    assessment: ImportanceAssessment
    received: EvaluationEvidence | None = None

    def evaluate(self, evidence: EvaluationEvidence) -> ImportanceAssessment:
        self.received = evidence
        return self.assessment


class FailingEvaluator:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def evaluate(self, evidence: EvaluationEvidence) -> ImportanceAssessment:
        del evidence
        raise self.error


class FailingReleaseSource:
    name = "github-releases"
    reference = "https://example.invalid/releases"

    def collect(self, versions: tuple[str, ...]) -> dict[str, ReleaseSourceNote]:
        del versions
        raise ReleaseCheckError("release source timed out")


class PiMcpReleaseCheckerTests(unittest.TestCase):
    def test_same_stable_version_completes_as_no_change_without_release_or_evaluator_calls(self) -> None:
        checker = PiMcpReleaseChecker(
            installed_versions=FakeInstalledVersionSource(
                InstallationEvidence(
                    adapter_version="2.26.1",
                    pi_version="0.84.2",
                    adapter_pi_requirement="^0.84.1",
                )
            ),
            registry=FakeRegistry(
                RegistrySnapshot(
                    latest_version="2.26.1",
                    stable_versions=("2.26.0", "2.26.1"),
                )
            ),
            release_sources=(UnexpectedReleaseSource(),),
            evaluator=UnexpectedEvaluator(),
        )

        result = checker.check(importance_context={})

        self.assertEqual(result.outcome, "no-change")
        self.assertEqual(result.installed_version, "2.26.1")
        self.assertEqual(result.available_version, "2.26.1")
        self.assertIsNone(result.assessment)
        self.assertEqual(result.evidence, ())

    def test_malformed_installed_or_registry_metadata_is_uncertain(self) -> None:
        cases = (
            (
                InstallationEvidence("not-a-version", "0.84.2", "^0.84.1"),
                RegistrySnapshot("2.27.0", ("2.27.0",)),
                "installed-package-metadata",
            ),
            (
                InstallationEvidence("2.26.1", "0.84.2", "^0.84.1"),
                RegistrySnapshot("3.0.0-beta.1", ("2.26.1",)),
                "package-registry",
            ),
        )
        for installed, registry, expected_failure in cases:
            with self.subTest(expected_failure=expected_failure):
                checker = PiMcpReleaseChecker(
                    installed_versions=FakeInstalledVersionSource(installed),
                    registry=FakeRegistry(registry),
                    release_sources=(UnexpectedReleaseSource(),),
                    evaluator=UnexpectedEvaluator(),
                )

                result = checker.check(importance_context={})

                self.assertEqual(result.outcome, "decision-required")
                assert result.assessment is not None
                self.assertEqual(result.assessment.importance, "uncertain")
                self.assertIn(expected_failure, result.source_failures)

    def test_every_intervening_registry_release_is_normalized_from_both_sources_before_evaluation(self) -> None:
        releases = FakeReleaseSource(
            name="github-releases",
            reference="https://github.com/example/releases",
            notes={
                "2.26.1": ReleaseSourceNote(
                    version="2.26.1",
                    title="v2.26.1",
                    body="Routine rendering feature.",
                    reference="https://github.com/example/releases/2.26.1",
                ),
                "2.27.0": ReleaseSourceNote(
                    version="2.27.0",
                    title="v2.27.0",
                    body="OAuth credential handling correction.",
                    reference="https://github.com/example/releases/2.27.0",
                ),
            },
        )
        changelog = FakeReleaseSource(
            name="changelog",
            reference="https://github.com/example/CHANGELOG.md",
            notes={
                "2.26.1": ReleaseSourceNote(
                    version="2.26.1",
                    title="2.26.1",
                    body="Routine rendering feature details.",
                    reference="https://github.com/example/CHANGELOG.md#2261",
                ),
                "2.27.0": ReleaseSourceNote(
                    version="2.27.0",
                    title="2.27.0",
                    body="OAuth credential handling correction details.",
                    reference="https://github.com/example/CHANGELOG.md#2270",
                ),
            },
        )
        evaluator = RecordingEvaluator(
            ImportanceAssessment(
                importance="important",
                category="credential-oauth",
                reason="The OAuth correction affects credential safety.",
                recommendation="Update after approval.",
                confidence="high",
            )
        )
        checker = PiMcpReleaseChecker(
            installed_versions=FakeInstalledVersionSource(
                InstallationEvidence("2.26.0", "0.84.2", "^0.84.1")
            ),
            registry=FakeRegistry(
                RegistrySnapshot(
                    latest_version="2.27.0",
                    stable_versions=("2.26.0", "2.26.1", "2.27.0"),
                )
            ),
            release_sources=(releases, changelog),
            evaluator=evaluator,
        )

        result = checker.check(importance_context={"active_mcp_servers": ["stripe"]})

        self.assertEqual(result.outcome, "decision-required")
        self.assertEqual(
            tuple(release.version for release in result.evidence),
            ("2.26.1", "2.27.0"),
        )
        self.assertEqual(releases.requested_versions, ("2.26.1", "2.27.0"))
        self.assertEqual(changelog.requested_versions, ("2.26.1", "2.27.0"))
        self.assertIsNotNone(evaluator.received)
        assert evaluator.received is not None
        self.assertEqual(len(evaluator.received.releases[0].sources), 2)
        self.assertEqual(
            evaluator.received.importance_context,
            {"active_mcp_servers": ["stripe"]},
        )

    def test_release_source_failure_and_missing_intervening_evidence_are_uncertain_without_evaluation(self) -> None:
        for source in (
            FailingReleaseSource(),
            FakeReleaseSource(
                name="github-releases",
                reference="https://example.invalid/releases",
                notes={},
            ),
        ):
            with self.subTest(source=source.__class__.__name__):
                checker = PiMcpReleaseChecker(
                    installed_versions=FakeInstalledVersionSource(
                        InstallationEvidence("2.26.1", "0.84.2", "^0.84.1")
                    ),
                    registry=FakeRegistry(
                        RegistrySnapshot("2.27.0", ("2.26.1", "2.27.0"))
                    ),
                    release_sources=(source,),
                    evaluator=UnexpectedEvaluator(),
                )

                result = checker.check(importance_context={})

                self.assertEqual(result.outcome, "decision-required")
                self.assertIsNotNone(result.assessment)
                assert result.assessment is not None
                self.assertEqual(result.assessment.importance, "uncertain")
                self.assertEqual(result.assessment.confidence, "low")
                self.assertTrue(result.source_failures)

    def test_malformed_evaluator_output_and_timeout_are_uncertain(self) -> None:
        source = FakeReleaseSource(
            name="changelog",
            reference="https://example.invalid/CHANGELOG.md",
            notes={
                "2.27.0": ReleaseSourceNote(
                    "2.27.0",
                    "2.27.0",
                    "Routine release details.",
                    "https://example.invalid/CHANGELOG.md#2270",
                )
            },
        )
        for error in (
            ReleaseCheckError("malformed evaluator output"),
            TimeoutError("evaluator timed out"),
        ):
            with self.subTest(error=error.__class__.__name__):
                checker = PiMcpReleaseChecker(
                    installed_versions=FakeInstalledVersionSource(
                        InstallationEvidence("2.26.1", "0.84.2", "^0.84.1")
                    ),
                    registry=FakeRegistry(
                        RegistrySnapshot("2.27.0", ("2.26.1", "2.27.0"))
                    ),
                    release_sources=(source,),
                    evaluator=FailingEvaluator(error),
                )

                result = checker.check(importance_context={})

                self.assertEqual(result.outcome, "decision-required")
                assert result.assessment is not None
                self.assertEqual(result.assessment.importance, "uncertain")
                self.assertIn("importance-evaluator", result.source_failures)

    def test_security_category_cannot_be_accepted_as_non_important(self) -> None:
        source = FakeReleaseSource(
            name="changelog",
            reference="https://example.invalid/CHANGELOG.md",
            notes={
                "2.27.0": ReleaseSourceNote(
                    "2.27.0",
                    "2.27.0",
                    "Security correction.",
                    "https://example.invalid/CHANGELOG.md#2270",
                )
            },
        )
        checker = PiMcpReleaseChecker(
            installed_versions=FakeInstalledVersionSource(
                InstallationEvidence("2.26.1", "0.84.2", "^0.84.1")
            ),
            registry=FakeRegistry(
                RegistrySnapshot("2.27.0", ("2.26.1", "2.27.0"))
            ),
            release_sources=(source,),
            evaluator=RecordingEvaluator(
                ImportanceAssessment(
                    "non-important",
                    "security",
                    "Contradictory unsafe classification.",
                    "Remain pinned.",
                    "high",
                )
            ),
        )

        result = checker.check(importance_context={})

        self.assertEqual(result.outcome, "decision-required")
        assert result.assessment is not None
        self.assertEqual(result.assessment.importance, "uncertain")

    def test_version_shape_never_overrides_evaluator_content_assessment(self) -> None:
        cases = (
            (
                "2.26.1",
                "2.26.2",
                ImportanceAssessment(
                    "important",
                    "security",
                    "Patch release fixes a relevant security defect.",
                    "Update after approval.",
                    "high",
                ),
                "decision-required",
            ),
            (
                "2.26.1",
                "3.0.0",
                ImportanceAssessment(
                    "non-important",
                    "routine",
                    "Major release contains only irrelevant documentation changes.",
                    "Remain pinned.",
                    "high",
                ),
                "non-important",
            ),
            (
                "2.26.1",
                "3.0.0",
                ImportanceAssessment(
                    "non-important",
                    "routine",
                    "Routine changes, but evidence confidence is incomplete.",
                    "Review manually.",
                    "medium",
                ),
                "decision-required",
            ),
        )
        for installed, available, assessment, expected in cases:
            with self.subTest(installed=installed, available=available):
                source = FakeReleaseSource(
                    name="changelog",
                    reference="https://example.invalid/CHANGELOG.md",
                    notes={
                        available: ReleaseSourceNote(
                            available,
                            available,
                            assessment.reason,
                            f"https://example.invalid/CHANGELOG.md#{available}",
                        )
                    },
                )
                checker = PiMcpReleaseChecker(
                    installed_versions=FakeInstalledVersionSource(
                        InstallationEvidence(installed, "0.84.2", "^0.84.1")
                    ),
                    registry=FakeRegistry(
                        RegistrySnapshot(available, (installed, available))
                    ),
                    release_sources=(source,),
                    evaluator=RecordingEvaluator(assessment),
                )

                result = checker.check(importance_context={})

                self.assertEqual(result.outcome, expected)


if __name__ == "__main__":
    unittest.main()
