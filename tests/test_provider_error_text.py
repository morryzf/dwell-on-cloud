import json
import unittest

from app.llm_client import provider_error_text

NEW_API = json.dumps({"error": {
    "message": "预扣费额度失败, 用户剩余额度: ¥0.200000, 需要预扣费额度: ¥0.350000 (request id: 20260923)",
    "type": "new_api_error", "param": "", "code": "insufficient_user_quota",
}}, ensure_ascii=False)


class ProviderErrorTextTest(unittest.TestCase):
    """余额不够是最常见的报错，要说成人话，别糊一段 JSON。"""

    def test_relay_quota_error_names_both_amounts(self):
        text = provider_error_text(403, NEW_API)
        self.assertTrue(text.startswith("[供应商错误 403] 供应商那边余额不够了"))
        self.assertIn("剩 ¥0.20，这条要预扣 ¥0.35", text)
        self.assertNotIn("new_api_error", text)

    def test_other_quota_wordings_are_recognised(self):
        openai = json.dumps({"error": {"message": "You exceeded your current quota", "code": "insufficient_quota"}})
        chinese = json.dumps({"message": "账户余额不足"}, ensure_ascii=False)
        for body in (openai, chinese, "insufficient balance"):
            self.assertIn("余额不够了", provider_error_text(402, body))

    def test_other_errors_show_the_providers_own_message(self):
        body = json.dumps({"error": {"message": "model not found", "code": "model_not_found"}})
        self.assertEqual(provider_error_text(404, body), "[供应商错误 404] model not found")
        self.assertEqual(provider_error_text(500, "boom"), "[供应商错误 500] boom")
        self.assertEqual(len(provider_error_text(500, "x" * 2000)), len("[供应商错误 500] ") + 500)


if __name__ == "__main__":
    unittest.main()
