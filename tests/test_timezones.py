"""Use synthetic RFC3339 timestamps; never inspect a user's Multica profiles."""
from contextlib import contextmanager
import os
from types import SimpleNamespace
import time
import unittest

from test_bridge import bridge


@contextmanager
def local_timezone(name):
    previous = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


class LocalTimestampTests(unittest.TestCase):
    def test_utc_to_user_local_time(self):
        with local_timezone("Europe/Moscow"):
            self.assertEqual("14:00:00", bridge.local_timestamp("2026-11-02T11:00:00Z"))
            self.assertEqual("2026-11-02 14:00", bridge.local_timestamp(
                "2026-11-02T11:00:00Z", include_date=True))

    def test_source_offsets_represent_the_same_instant(self):
        with local_timezone("Europe/Moscow"):
            for stamp in ["2026-11-02T11:00:00Z", "2026-11-02T11:00:00+00:00",
                          "2026-11-02T14:00:00+03:00", "2026-11-02T06:00:00-05:00",
                          "2026-11-02T16:30:00+05:30"]:
                with self.subTest(stamp=stamp):
                    self.assertEqual("14:00:00", bridge.local_timestamp(stamp))

    def test_fractional_seconds_including_go_nanoseconds(self):
        with local_timezone("Europe/Moscow"):
            for fraction in ["1", "12", "123", "1234", "12345", "123456", "123456789"]:
                with self.subTest(fraction=fraction):
                    self.assertEqual("14:00:09", bridge.local_timestamp(
                        "2026-11-02T11:00:09." + fraction + "Z"))

    def test_day_year_and_negative_offset_boundaries(self):
        cases = [
            ("Europe/Moscow", "2026-12-31T22:30:00Z", "2027-01-01 01:30"),
            ("America/New_York", "2026-01-01T02:00:00Z", "2025-12-31 21:00"),
            ("Asia/Kolkata", "2026-11-02T11:00:00Z", "2026-11-02 16:30"),
            ("UTC", "2026-11-02T14:00:00+03:00", "2026-11-02 11:00"),
        ]
        for zone, stamp, expected in cases:
            with self.subTest(zone=zone), local_timezone(zone):
                self.assertEqual(expected, bridge.local_timestamp(stamp, include_date=True))

    def test_dst_uses_the_timestamp_date_not_the_current_offset(self):
        with local_timezone("America/New_York"):
            for stamp, expected in [
                ("2026-01-15T12:00:00Z", "07:00:00"),
                ("2026-07-15T12:00:00Z", "08:00:00"),
                ("2026-03-08T06:59:59Z", "01:59:59"),
                ("2026-03-08T07:00:00Z", "03:00:00"),
                ("2026-11-01T05:59:59Z", "01:59:59"),
                ("2026-11-01T06:00:00Z", "01:00:00"),
            ]:
                with self.subTest(stamp=stamp):
                    self.assertEqual(expected, bridge.local_timestamp(stamp))

    def test_empty_invalid_and_unzoned_values_do_not_invent_times(self):
        values = [None, "", 0, {}, "bad", "2026-11-02", "2026-11-02T11:00:00",
                  "2026-02-30T11:00:00Z", "2026-11-02T25:00:00Z",
                  "2026-11-02T11:00:00+25:00", "2026-11-02T11:00:00+03:99",
                  "2026-11-02T11:00:00Z trailing"]
        with local_timezone("Europe/Moscow"):
            for value in values:
                with self.subTest(value=value):
                    self.assertEqual("", bridge.local_timestamp(value))
                    self.assertEqual("", bridge.local_timestamp(value, include_date=True))

    def test_extreme_date_cannot_crash_the_panel(self):
        with local_timezone("Europe/Moscow"):
            self.assertEqual("", bridge.local_timestamp("9999-12-31T23:59:59Z"))

    def test_subsequent_bridge_requests_use_the_new_system_zone(self):
        stamp = "2026-11-02T11:00:00Z"
        for zone, expected in [("UTC", "11:00:00"), ("Europe/Moscow", "14:00:00")]:
            with local_timezone(zone):
                self.assertEqual(expected, bridge.local_timestamp(stamp))

    def test_task_card_keeps_original_timestamp_and_adds_local_date(self):
        client = SimpleNamespace(name="", app="https://example.test")
        stamp = "2026-12-31T22:30:00.123456789Z"
        task = {"id": "task", "agent_id": "agent", "status": "completed", "completed_at": stamp}
        with local_timezone("Europe/Moscow"):
            result = bridge.task_summary(client, {"id": "workspace"}, task, {})
        self.assertEqual(stamp, result["completed_at"])
        self.assertEqual("2027-01-01 01:30", result["completed_at_local"])
        self.assertNotIn("completed_at_local", task)

    def test_log_display_does_not_change_sequences_or_raw_timestamps(self):
        stamp = "2026-11-02T11:00:00Z"
        source = [{"seq": 1, "type": "text", "content": "Synthetic", "created_at": stamp},
                  {"seq": 2, "type": "tool_use", "content": "hidden", "created_at": stamp}]
        with local_timezone("Europe/Moscow"):
            page = bridge.log_page(source, since=0)
        self.assertEqual(2, page["last_seq"])
        self.assertEqual([1], [row["seq"] for row in page["messages"]])
        self.assertEqual(stamp, page["messages"][0]["created_at"])
        self.assertEqual("14:00:00", page["messages"][0]["created_at_local"])
        self.assertNotIn("created_at_local", source[0])


if __name__ == "__main__":
    unittest.main()
