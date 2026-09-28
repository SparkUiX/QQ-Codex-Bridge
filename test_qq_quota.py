import unittest
from unittest.mock import patch

from qq_quota import format_quota


class QuotaTests(unittest.TestCase):
    def test_two_windows_render_remaining_and_reset(self):
        result = {"rateLimits": {"limitId": "codex",
            "primary": {"usedPercent": 53, "windowDurationMins": 300, "resetsAt": 1780000000},
            "secondary": {"usedPercent": 11, "windowDurationMins": 10080, "resetsAt": 1780100000}}}
        with patch("qq_quota.datetime") as clock:
            clock.fromtimestamp.return_value.strftime.return_value = "10月04日 05:01"
            text = format_quota(result)
        self.assertIn("5 小时", text)
        self.assertIn("剩余 47%", text)
        self.assertIn("7 天", text)
        self.assertIn("剩余 89%", text)
        self.assertIn("10月04日 05:01", text)


if __name__ == "__main__":
    unittest.main()
