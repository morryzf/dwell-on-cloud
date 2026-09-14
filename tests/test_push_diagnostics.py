import asyncio
import unittest
from unittest.mock import patch

from app import push_service


class PushDiagnosticsTest(unittest.TestCase):
    def test_vapid_subject_never_falls_back_to_localhost(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                push_service._vapid_subject(),
                "https://github.com/morryzf/dwell-on-cloud",
            )
        with patch.dict("os.environ", {"VAPID_SUBJECT": "mailto:dwell@localhost"}):
            self.assertEqual(
                push_service._vapid_subject(),
                "https://github.com/morryzf/dwell-on-cloud",
            )

    def test_vapid_subject_accepts_a_public_contact_uri(self):
        with patch.dict("os.environ", {"VAPID_SUBJECT": "mailto:push@example.com"}):
            self.assertEqual(push_service._vapid_subject(), "mailto:push@example.com")

    def test_failure_exposes_only_host_and_exception_kind(self):
        subscription = {
            "endpoint": "https://web.push.apple.com/QH/private-token?secret=yes",
            "keys": {"p256dh": "private-key", "auth": "private-auth"},
        }
        with (
            patch.object(push_service, "_subscriptions", return_value=[subscription]),
            patch.object(push_service, "ensure_vapid_keys", return_value=("public", "private")),
            patch.object(push_service, "webpush", side_effect=RuntimeError("network blocked")),
        ):
            result = push_service._send_sync("title", "body", "/")

        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertIn("web.push.apple.com", result["diagnostic"])
        self.assertIn("RuntimeError", result["diagnostic"])
        self.assertNotIn("private-token", result["diagnostic"])
        self.assertNotIn("private-key", result["diagnostic"])

    def test_missing_subscription_is_written_to_system_log(self):
        with (
            patch.object(push_service, "_subscriptions", return_value=[]),
            patch.object(push_service.db, "system_log_start", return_value="log-1"),
            patch.object(push_service.db, "system_log_finish") as finish,
        ):
            result = asyncio.run(push_service.send_push("title", "body"))

        self.assertEqual(result["subscriptions"], 0)
        finish.assert_called_once()
        self.assertEqual(finish.call_args.args[1], "error")
        self.assertIn("没有已保存", finish.call_args.kwargs["detail"])


if __name__ == "__main__":
    unittest.main()
