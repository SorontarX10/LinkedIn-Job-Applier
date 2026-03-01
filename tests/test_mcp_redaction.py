from __future__ import annotations

import unittest

from src.mcp_bridge.redaction import redact_payload, redact_text


class McpRedactionTest(unittest.TestCase):
    def test_redacts_email_phone_and_api_key(self) -> None:
        text = "Contact me at test.user@example.com or +48 669 847 528. Key=sk-ABCDEF1234567890ABCDEF1234567890"
        redacted = redact_text(text)
        self.assertNotIn("test.user@example.com", redacted)
        self.assertNotIn("+48 669 847 528", redacted)
        self.assertNotIn("sk-ABCDEF1234567890ABCDEF1234567890", redacted)
        self.assertIn("[redacted_email]", redacted)
        self.assertIn("[redacted_phone]", redacted)

    def test_redacts_sensitive_keys_in_payload(self) -> None:
        payload = {
            "email": "john@example.com",
            "job_url": "https://www.linkedin.com/jobs/view/123",
            "raw_html": "<html>" + ("x" * 3000) + "</html>",
            "nested": {"api_key": "secret-value"},
        }
        redacted = redact_payload(payload)
        self.assertEqual(redacted["nested"]["api_key"], "[redacted_sensitive_value]")
        self.assertTrue(str(redacted["raw_html"]).startswith("[redacted_html_sha16:"))
        self.assertNotIn("john@example.com", str(redacted))


if __name__ == "__main__":
    unittest.main()

