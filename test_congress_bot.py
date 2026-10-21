import unittest
from datetime import datetime, timezone

import congress_bot


class RecentActivityTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)

    def bill(self, action_date, action_text="Referred to the Committee", introduced="2026-10-21"):
        return {
            "congress": 119,
            "type": "HR",
            "number": 1234,
            "introducedDate": introduced,
            "latestAction": {"actionDate": action_date, "text": action_text},
        }

    def test_today_date_only_is_accepted(self):
        self.assertTrue(congress_bot.is_recent_activity(self.bill("2026-10-21"), self.now))

    def test_yesterday_date_only_is_rejected_without_timestamp(self):
        self.assertFalse(congress_bot.is_recent_activity(self.bill("2026-10-20"), self.now))

    def test_previous_calendar_day_date_only_is_rejected(self):
        self.assertFalse(congress_bot.is_recent_activity(self.bill("2026-10-19"), self.now))

    def test_2025_action_is_rejected(self):
        self.assertFalse(congress_bot.is_recent_activity(self.bill("2025-12-31"), self.now))

    def test_timestamp_within_last_24_hours_is_accepted(self):
        self.assertTrue(
            congress_bot.is_recent_activity(
                self.bill("2026-10-20T11:00:00Z"), self.now
            )
        )

    def test_timestamp_older_than_24_hours_is_rejected(self):
        self.assertFalse(
            congress_bot.is_recent_activity(
                self.bill("2026-10-20T09:59:59Z"), self.now
            )
        )

    def test_future_timestamp_is_rejected(self):
        self.assertFalse(
            congress_bot.is_recent_activity(
                self.bill("2026-10-21T10:00:01Z"), self.now
            )
        )

    def test_fingerprint_ignores_action_text_whitespace_and_case(self):
        a = self.bill("2026-10-21", "Referred   to the Committee")
        b = self.bill("2026-10-21", "REFERRED to the committee")
        self.assertEqual(
            congress_bot.get_action_fingerprint(a),
            congress_bot.get_action_fingerprint(b),
        )

    def test_fingerprint_changes_when_action_changes(self):
        a = self.bill("2026-10-21", "Referred to the Committee")
        b = self.bill("2026-10-21", "Passed House")
        self.assertNotEqual(
            congress_bot.get_action_fingerprint(a),
            congress_bot.get_action_fingerprint(b),
        )


if __name__ == "__main__":
    unittest.main()
