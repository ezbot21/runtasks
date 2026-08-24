from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Mapping, Protocol, cast


MAX_RELEASE_BODY_CHARS = 12_000
MAX_NORMALIZED_EVIDENCE_CHARS = 24_000
_IMPORTANCE_VALUES = frozenset({"important", "non-important", "uncertain"})
_CONFIDENCE_VALUES = frozenset({"high", "medium", "low"})
_CATEGORY_VALUES = frozenset(
    {
        "security",
        "credential-oauth",
        "pi-compatibility",
        "active-server-breakage",
        "protocol-connection",
        "approval-output-safety",
        "serious-operational-defect",
        "routine",
        "uncertain",
    }
)


class ReleaseCheckError(RuntimeError):
    """Raised when release evidence cannot be obtained or normalized safely."""


@dataclass(frozen=True)
class InstallationEvidence:
    adapter_version: str
    pi_version: str | None
    adapter_pi_requirement: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "adapter_pi_requirement": self.adapter_pi_requirement,
            "adapter_version": self.adapter_version,
            "pi_version": self.pi_version,
        }


@dataclass(frozen=True)
class RegistrySnapshot:
    latest_version: str
    stable_versions: tuple[str, ...]
    available_pi_requirement: str | None = None


@dataclass(frozen=True)
class ImportanceAssessment:
    importance: str
    category: str
    reason: str
    recommendation: str
    confidence: str

    def as_dict(self) -> dict[str, str]:
        return {
            "category": self.category,
            "confidence": self.confidence,
            "importance": self.importance,
            "reason": self.reason,
            "recommendation": self.recommendation,
        }


@dataclass(frozen=True)
class ReleaseSourceNote:
    version: str
    title: str
    body: str
    reference: str


@dataclass(frozen=True)
class NormalizedSourceEvidence:
    source: str
    title: str
    body: str
    reference: str

    def as_dict(self) -> dict[str, str]:
        return {
            "body": self.body,
            "reference": self.reference,
            "source": self.source,
            "title": self.title,
        }


@dataclass(frozen=True)
class NormalizedRelease:
    version: str
    sources: tuple[NormalizedSourceEvidence, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "sources": [source.as_dict() for source in self.sources],
            "version": self.version,
        }


@dataclass(frozen=True)
class EvaluationEvidence:
    installed_version: str
    available_version: str
    pi_version: str | None
    adapter_pi_requirement: str | None
    importance_context: Mapping[str, object]
    releases: tuple[NormalizedRelease, ...]
    available_pi_requirement: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "adapter_pi_requirement": self.adapter_pi_requirement,
            "available_pi_requirement": self.available_pi_requirement,
            "available_version": self.available_version,
            "importance_context": dict(self.importance_context),
            "installed_version": self.installed_version,
            "pi_version": self.pi_version,
            "releases": [release.as_dict() for release in self.releases],
        }


@dataclass(frozen=True)
class ReleaseCheckResult:
    outcome: str
    installed_version: str | None
    available_version: str | None
    assessment: ImportanceAssessment | None
    evidence: tuple[NormalizedRelease, ...]
    source_failures: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "assessment": (
                None if self.assessment is None else self.assessment.as_dict()
            ),
            "available_version": self.available_version,
            "contract": "pi-mcp-release-check/v1",
            "evidence": [release.as_dict() for release in self.evidence],
            "installed_version": self.installed_version,
            "outcome": self.outcome,
            "source_failures": list(self.source_failures),
        }


class InstalledVersionSource(Protocol):
    def detect(self) -> InstallationEvidence: ...


class PackageRegistry(Protocol):
    def lookup(self) -> RegistrySnapshot: ...


class ReleaseSource(Protocol):
    name: str
    reference: str

    def collect(
        self, versions: tuple[str, ...]
    ) -> Mapping[str, ReleaseSourceNote]: ...


class ImportanceEvaluator(Protocol):
    def evaluate(self, evidence: EvaluationEvidence) -> ImportanceAssessment: ...


class PiMcpReleaseChecker:
    """Deep read-only module for the complete Pi MCP release-check policy."""

    def __init__(
        self,
        *,
        installed_versions: InstalledVersionSource,
        registry: PackageRegistry,
        release_sources: tuple[ReleaseSource, ...],
        evaluator: ImportanceEvaluator,
    ) -> None:
        if not release_sources:
            raise ValueError("at least one release source is required")
        self._installed_versions = installed_versions
        self._registry = registry
        self._release_sources = release_sources
        self._evaluator = evaluator

    def check(self, importance_context: Mapping[str, object]) -> ReleaseCheckResult:
        try:
            installed = self._installed_versions.detect()
            installed_version = _stable_semantic_version(installed.adapter_version)
        except (ReleaseCheckError, OSError, TimeoutError, ValueError):
            return _uncertain_result(
                installed_version=None,
                available_version=None,
                reason="Installed Pi MCP adapter metadata could not be validated.",
                source_failures=("installed-package-metadata",),
            )

        try:
            registry = self._registry.lookup()
            latest_version = _stable_semantic_version(registry.latest_version)
            stable_versions = _validated_stable_versions(registry.stable_versions)
            if latest_version not in stable_versions:
                raise ReleaseCheckError(
                    "registry latest version is absent from stable versions"
                )
            if latest_version.precedence < installed_version.precedence:
                raise ReleaseCheckError(
                    "registry latest version is older than installed version"
                )
        except (ReleaseCheckError, OSError, TimeoutError, ValueError):
            return _uncertain_result(
                installed_version=str(installed_version),
                available_version=None,
                reason="The package registry stable release metadata could not be validated.",
                source_failures=("package-registry",),
            )

        if installed_version == latest_version:
            return ReleaseCheckResult(
                outcome="no-change",
                installed_version=str(installed_version),
                available_version=str(latest_version),
                assessment=None,
                evidence=(),
            )

        intervening = tuple(
            str(version)
            for version in stable_versions
            if installed_version.precedence < version.precedence <= latest_version.precedence
        )
        if not intervening or intervening[-1] != str(latest_version):
            return _uncertain_result(
                installed_version=str(installed_version),
                available_version=str(latest_version),
                reason="The registry did not provide every intervening stable release.",
                source_failures=("package-registry-intervening-releases",),
            )

        releases, source_failures = self._collect_release_evidence(intervening)
        context_failures = (
            ("importance-context:pi-version",)
            if installed.pi_version is None
            else ()
        )
        assessment_failures = (*source_failures, *context_failures)
        if assessment_failures:
            return _uncertain_result(
                installed_version=str(installed_version),
                available_version=str(latest_version),
                reason=(
                    "Intervening release evidence is unavailable or incomplete; "
                    "human review is required."
                ),
                evidence=releases,
                source_failures=assessment_failures,
            )

        evaluation_evidence = EvaluationEvidence(
            installed_version=str(installed_version),
            available_version=str(latest_version),
            pi_version=installed.pi_version,
            adapter_pi_requirement=installed.adapter_pi_requirement,
            importance_context=dict(importance_context),
            releases=releases,
            available_pi_requirement=registry.available_pi_requirement,
        )
        try:
            assessment = _validated_assessment(
                self._evaluator.evaluate(evaluation_evidence)
            )
        except (ReleaseCheckError, OSError, TimeoutError, ValueError):
            return _uncertain_result(
                installed_version=str(installed_version),
                available_version=str(latest_version),
                reason=(
                    "The semantic evaluator did not return a safe structured "
                    "assessment; human review is required."
                ),
                evidence=releases,
                source_failures=("importance-evaluator",),
            )

        if assessment.confidence == "low" or (
            assessment.importance == "non-important"
            and assessment.confidence != "high"
        ):
            return _uncertain_result(
                installed_version=str(installed_version),
                available_version=str(latest_version),
                reason=(
                    "The evaluator's confidence is insufficient to classify the "
                    "release as safely non-important."
                ),
                evidence=releases,
                source_failures=("low-confidence-assessment",),
            )

        outcome = (
            "non-important"
            if assessment.importance == "non-important"
            else "decision-required"
        )
        return ReleaseCheckResult(
            outcome=outcome,
            installed_version=str(installed_version),
            available_version=str(latest_version),
            assessment=assessment,
            evidence=releases,
        )

    def _collect_release_evidence(
        self,
        versions: tuple[str, ...],
    ) -> tuple[tuple[NormalizedRelease, ...], tuple[str, ...]]:
        normalized: dict[str, list[NormalizedSourceEvidence]] = {
            version: [] for version in versions
        }
        failures: list[str] = []
        total_chars = 0
        for source in self._release_sources:
            try:
                notes = source.collect(versions)
            except (ReleaseCheckError, OSError, TimeoutError, ValueError):
                failures.append(f"{source.name}:unavailable")
                continue
            for version in versions:
                note = notes.get(version)
                if note is None:
                    failures.append(f"{source.name}:missing:{version}")
                    continue
                try:
                    evidence = _normalize_source_note(source.name, version, note)
                except ReleaseCheckError:
                    failures.append(f"{source.name}:malformed:{version}")
                    continue
                total_chars += sum(
                    len(value)
                    for value in (
                        evidence.source,
                        evidence.title,
                        evidence.body,
                        evidence.reference,
                    )
                )
                normalized[version].append(evidence)
        if total_chars > MAX_NORMALIZED_EVIDENCE_CHARS:
            failures.append("release-evidence:oversized")
            return (), tuple(dict.fromkeys(failures))
        releases = tuple(
            NormalizedRelease(version=version, sources=tuple(normalized[version]))
            for version in versions
        )
        return releases, tuple(dict.fromkeys(failures))


_SEMVER = re.compile(
    r"(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?(?:\+(?P<build>[0-9A-Za-z.-]+))?\Z"
)


@dataclass(frozen=True)
class _SemanticVersion:
    major: int
    minor: int
    patch: int
    original: str

    @property
    def precedence(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def __str__(self) -> str:
        return self.original


def _stable_semantic_version(value: str) -> _SemanticVersion:
    if not isinstance(value, str):
        raise ValueError("version must be text")
    match = _SEMVER.fullmatch(value.strip())
    if match is None or match.group("prerelease") is not None:
        raise ValueError("version must be a stable semantic version")
    return _SemanticVersion(
        major=int(cast(str, match.group("major"))),
        minor=int(cast(str, match.group("minor"))),
        patch=int(cast(str, match.group("patch"))),
        original=value.strip(),
    )


def _validated_stable_versions(values: tuple[str, ...]) -> tuple[_SemanticVersion, ...]:
    if not values:
        raise ReleaseCheckError("registry stable versions are empty")
    parsed = tuple(_stable_semantic_version(value) for value in values)
    if len({version.original for version in parsed}) != len(parsed):
        raise ReleaseCheckError("registry stable versions contain duplicates")
    return tuple(sorted(parsed, key=lambda version: version.precedence))


def _normalize_source_note(
    source_name: str,
    expected_version: str,
    note: ReleaseSourceNote,
) -> NormalizedSourceEvidence:
    if note.version != expected_version:
        raise ReleaseCheckError("release source version does not match")
    fields = (source_name, note.title, note.body, note.reference)
    if any(not isinstance(value, str) or not value.strip() for value in fields):
        raise ReleaseCheckError("release source note is incomplete")
    title = note.title.strip()
    body = note.body.strip()
    reference = note.reference.strip()
    if len(title) > 1_000 or len(body) > MAX_RELEASE_BODY_CHARS:
        raise ReleaseCheckError("release source note exceeds evidence limits")
    if len(reference) > 2_000:
        raise ReleaseCheckError("release source reference is too long")
    return NormalizedSourceEvidence(
        source=source_name.strip(),
        title=title,
        body=body,
        reference=reference,
    )


def _validated_assessment(value: ImportanceAssessment) -> ImportanceAssessment:
    if not isinstance(value, ImportanceAssessment):
        raise ReleaseCheckError("importance assessment is malformed")
    if value.importance not in _IMPORTANCE_VALUES:
        raise ReleaseCheckError("importance assessment value is invalid")
    if value.category not in _CATEGORY_VALUES:
        raise ReleaseCheckError("importance assessment category is invalid")
    if value.confidence not in _CONFIDENCE_VALUES:
        raise ReleaseCheckError("importance assessment confidence is invalid")
    for text in (value.reason, value.recommendation):
        if not isinstance(text, str) or not text.strip() or len(text) > 8_000:
            raise ReleaseCheckError("importance assessment text is invalid")
    if value.importance == "uncertain" and value.category != "uncertain":
        raise ReleaseCheckError("uncertain assessment category is invalid")
    if value.importance == "non-important" and value.category != "routine":
        raise ReleaseCheckError("non-important assessment category is invalid")
    if value.importance == "important" and value.category in {"routine", "uncertain"}:
        raise ReleaseCheckError("important assessment category is invalid")
    return ImportanceAssessment(
        importance=value.importance,
        category=value.category,
        reason=value.reason.strip(),
        recommendation=value.recommendation.strip(),
        confidence=value.confidence,
    )


def normalize_evaluator_output(value: object) -> ImportanceAssessment:
    """Validate the stable evaluator interface independently of any agent adapter."""
    if not isinstance(value, dict):
        raise ReleaseCheckError("evaluator output must be an object")
    output = cast(dict[object, object], value)
    expected = {
        "importance",
        "category",
        "reason",
        "recommendation",
        "confidence",
    }
    if set(output) != expected:
        raise ReleaseCheckError("evaluator output fields are invalid")
    if not all(isinstance(key, str) for key in output):
        raise ReleaseCheckError("evaluator output fields are invalid")
    raw = cast(dict[str, object], output)
    values = tuple(raw[name] for name in expected)
    if not all(isinstance(item, str) for item in values):
        raise ReleaseCheckError("evaluator output values are invalid")
    return _validated_assessment(
        ImportanceAssessment(
            importance=cast(str, raw["importance"]),
            category=cast(str, raw["category"]),
            reason=cast(str, raw["reason"]),
            recommendation=cast(str, raw["recommendation"]),
            confidence=cast(str, raw["confidence"]),
        )
    )


def canonical_evidence_json(evidence: EvaluationEvidence) -> str:
    return json.dumps(
        evidence.as_dict(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _uncertain_result(
    *,
    installed_version: str | None,
    available_version: str | None,
    reason: str,
    evidence: tuple[NormalizedRelease, ...] = (),
    source_failures: tuple[str, ...],
) -> ReleaseCheckResult:
    return ReleaseCheckResult(
        outcome="decision-required",
        installed_version=installed_version,
        available_version=available_version,
        assessment=ImportanceAssessment(
            importance="uncertain",
            category="uncertain",
            reason=reason,
            recommendation="Review the evidence manually; do not update automatically.",
            confidence="low",
        ),
        evidence=evidence,
        source_failures=source_failures,
    )
