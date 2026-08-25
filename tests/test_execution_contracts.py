from __future__ import annotations

import unittest

from runtasks.adapters import ExternalAdapterError, normalize_external_outcome
from runtasks.one_shot import SystemdOneShotRunTrigger
from runtasks.redaction import Redactor


class ExternalAdapterContractTests(unittest.TestCase):
    def test_outcomes_are_normalized_and_redacted_at_the_adapter_boundary(self) -> None:
        private_value = "plain-private-value"
        outcome = normalize_external_outcome(
            {
                "status": "success",
                "summary": f"Inspected {private_value} with Bearer abcdefghijklmnop",
                "details": {
                    "credential": private_value,
                    "validation": f"Accepted {private_value}",
                },
                "external_log_ref": "https://user:password@example.invalid/log",
            },
            Redactor.from_secret_values([private_value]),
        )

        self.assertEqual(outcome.status, "success")
        self.assertEqual(
            outcome.summary,
            "Inspected [REDACTED] with [REDACTED]",
        )
        self.assertEqual(outcome.details["credential"], "[REDACTED]")
        self.assertEqual(outcome.details["validation"], "Accepted [REDACTED]")
        self.assertEqual(outcome.external_log_ref, "https://example.invalid/log")

    def test_malformed_outcomes_are_rejected_without_process_details_leaking(self) -> None:
        invalid_outcomes = (
            "raw process output",
            {"status": "unknown", "summary": "Result", "details": {}},
            {"status": "success", "summary": "", "details": {}},
            {"status": "success", "summary": "Result", "details": []},
            {
                "status": "success",
                "summary": "Result",
                "details": {},
                "stdout": "unbounded process output",
            },
        )

        for outcome in invalid_outcomes:
            with self.subTest(outcome=outcome):
                with self.assertRaises(ExternalAdapterError):
                    normalize_external_outcome(outcome, Redactor())


class RecordingSystemdOneShotRunTrigger(SystemdOneShotRunTrigger):
    def __init__(self, results: list[int]) -> None:
        super().__init__(timeout_seconds=2)
        self._results = list(results)
        self.calls: list[tuple[str, ...]] = []

    async def _run_systemctl(self, *arguments: str) -> int:
        self.calls.append(arguments)
        return self._results.pop(0)


class OneShotRunTriggerContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_active_scheduler_before_queueing_a_fresh_run(self) -> None:
        trigger = RecordingSystemdOneShotRunTrigger([0, 3, 0])

        await trigger.request()

        self.assertEqual(
            trigger.calls,
            [
                ("is-active", "--quiet", "runtasks-scheduler.service"),
                ("is-active", "--quiet", "runtasks-scheduler.service"),
                ("start", "--no-block", "runtasks-scheduler.service"),
            ],
        )


class RedactionContractTests(unittest.TestCase):
    def test_redaction_recurses_and_covers_common_credential_shapes(self) -> None:
        redactor = Redactor.from_secret_values(["configured-private-value"])

        redacted = redactor.value(
            {
                "message": "configured-private-value ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
                "configured-private-value": "evidence-key-secret",
                "nested": [
                    {"access_token": "anything", "api_key": "short-value"},
                    "Bearer abcdefghijklmnop",
                ],
            }
        )

        self.assertEqual(
            redacted,
            {
                "message": "[REDACTED] [REDACTED]",
                "[REDACTED]": "evidence-key-secret",
                "nested": [
                    {
                        "access_token": "[REDACTED]",
                        "api_key": "[REDACTED]",
                    },
                    "[REDACTED]",
                ],
            },
        )


if __name__ == "__main__":
    unittest.main()
