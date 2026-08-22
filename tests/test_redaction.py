from __future__ import annotations

import io
import logging
from pathlib import Path
import tempfile
import unittest

from runtasks.paths import RuntimePaths
from runtasks.redaction import (
    RedactingLogFilter,
    install_redacting_log_filter,
    redact_text,
)
from runtasks.secrets import load_secret_settings


TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"


class RedactionTests(unittest.TestCase):
    def test_credential_named_process_environment_values_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = RuntimePaths.from_environment(
                {
                    "HOME": str(root),
                    "RUNTASKS_HOME": str(root / "runtime"),
                },
                global_lock_directory=root / "locks",
            )
            values = load_secret_settings(
                paths,
                {
                    "AWS_SECRET_ACCESS_KEY": "aws-private-value",
                    "ORDINARY_SETTING": "public-value",
                },
            )

        self.assertEqual(values["AWS_SECRET_ACCESS_KEY"], "aws-private-value")
        self.assertNotIn("ORDINARY_SETTING", values)

    def test_redaction_removes_credentials_environment_values_and_sensitive_urls(self) -> None:
        text = (
            f"token={TOKEN} RUNTASKS_SAMPLE=private-environment-value short=xy "
            "OTHER_PASSWORD=generic-credential password: colon-secret "
            "password = spaced-secret {\"password\":\"json-secret\"} "
            "Authorization: Bearer bearer-secret "
            "Authorization = Bearer equals-bearer "
            "-----BEGIN PRIVATE KEY-----\nprivate-key-body\n"
            "-----END PRIVATE KEY----- "
            f"https://user:password@example.test/path?q={TOKEN} "
            f"ssh://user:credential@example.test/private/key "
            "http://example.test:bad/private/path "
            "https://docs.example.test/public/guide "
            f"https://api.telegram.org/bot{TOKEN}/getUpdates"
        )

        redacted = redact_text(
            text,
            sensitive_values=(TOKEN, "private-environment-value", "xy"),
        )

        self.assertNotIn(TOKEN, redacted)
        self.assertNotIn("private-environment-value", redacted)
        self.assertNotIn("generic-credential", redacted)
        self.assertNotIn("colon-secret", redacted)
        self.assertNotIn("spaced-secret", redacted)
        self.assertNotIn("json-secret", redacted)
        self.assertNotIn("bearer-secret", redacted)
        self.assertNotIn("equals-bearer", redacted)
        self.assertNotIn("private-key-body", redacted)
        self.assertNotIn("short=xy", redacted)
        self.assertNotIn("user:password", redacted)
        self.assertNotIn("user:credential", redacted)
        self.assertNotIn(":bad", redacted)
        self.assertIn("https://docs.example.test/public/guide", redacted)
        self.assertIn("https://api.telegram.org/", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_future_loggers_and_handlers_receive_redacted_records(self) -> None:
        install_redacting_log_filter(sensitive_values=(TOKEN, "998877665"))
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        handler.setFormatter(
            logging.Formatter(
                "%(message)s %(token)s %(url)s %(context)s %(chat_id)s "
                "%(object_value)s"
            )
        )
        logger = logging.getLogger("telegram.created.after.install")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.ERROR)

        logger.error(
            "request %s OTHER_PASSWORD=generic-secret",
            f"https://api.telegram.org/bot{TOKEN}/getUpdates",
            extra={
                "token": TOKEN,
                "url": f"https://internal.example/bot{TOKEN}/updates",
                "context": {
                    f"key-{TOKEN}": "safe",
                    "password": "structured-secret",
                },
                "chat_id": 998877665,
                "object_value": RuntimeError(TOKEN),
            },
        )

        logged = output.getvalue()
        self.assertNotIn(TOKEN, logged)
        self.assertIn("https://api.telegram.org/", logged)
        self.assertNotIn("generic-secret", logged)
        self.assertIn("https://internal.example/", logged)
        self.assertNotIn("structured-secret", logged)
        self.assertNotIn("998877665", logged)

    def test_log_filter_redacts_values_urls_and_exception_text(self) -> None:
        record = logging.LogRecord(
            name="telegram.request",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg=(
                f"request https://api.telegram.org/bot{TOKEN}/getUpdates "
                "OTHER_SECRET=generic-secret"
            ),
            args=(),
            exc_info=(RuntimeError, RuntimeError(TOKEN), None),
        )

        allowed = RedactingLogFilter(sensitive_values=(TOKEN,)).filter(record)

        self.assertTrue(allowed)
        self.assertNotIn(TOKEN, str(record.msg))
        self.assertNotIn("generic-secret", str(record.msg))
        self.assertIsNone(record.exc_info)
        self.assertIsNotNone(record.exc_text)
        self.assertIn("[REDACTED]", str(record.exc_text))
        self.assertNotIn(TOKEN, str(record.exc_text))

    def test_log_filter_preserves_non_sensitive_exception_diagnostics(self) -> None:
        exception_info = (RuntimeError, RuntimeError("safe failure"), None)
        record = logging.LogRecord(
            name="telegram.request",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="safe message",
            args=(),
            exc_info=exception_info,
        )

        RedactingLogFilter(sensitive_values=(TOKEN,)).filter(record)

        self.assertEqual(record.exc_info, exception_info)
        self.assertIsNone(record.exc_text)


if __name__ == "__main__":
    unittest.main()
