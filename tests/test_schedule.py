import tempfile
import unittest
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main
from weather_risk import SourceResult, WeatherState


class FixedDatetime(datetime):
    @classmethod
    def now(cls):
        return cls(2026, 10, 12, 10, 20)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        pause = patch.object(main, "PAUSE_FILE", Path(self.temp.name) / "pause")
        pause.start()
        self.addCleanup(pause.stop)
        clock = patch.object(main, "datetime", FixedDatetime)
        clock.start()
        self.addCleanup(clock.stop)
        timers = patch.object(main, "NSTimer")
        self.timers = timers.start()
        self.addCleanup(timers.stop)
        self.app = object.__new__(main.SlackStatusApp)
        self.app.status_schedule = main.load_status_schedule()
        self.app.status_presets = main.load_status_presets()
        self.app.timer = None
        self.app.next_auto_update_at = None
        self.app.schedule_timer_delegate = Mock()
        self.app.panel_pause_status_label = None
        self.app.panel_next_change_label = None
        self.app.default_status_button = None
        self.app.update_panel_status = Mock()
        self.app.set_slack_status = Mock(return_value=True)
        self.app.weather_state = WeatherState()
        self.app.start_weather_refresh = Mock()
        self.app.update_weather_panel = Mock()
        self.app.last_applied_status = None
        self.app.vacation_mode = False
        self.app.refresh_vacation_controls = Mock()
        vacation = patch.object(main, "VACATION_FILE", Path(self.temp.name) / "settings" / "vacation.json")
        vacation.start()
        self.addCleanup(vacation.stop)

    def test_off_hour_launch_targets_actual_boundary(self):
        self.app.update_status(notify=False)
        self.assertEqual(self.app.next_auto_update_at, datetime(2026, 10, 12, 15))
        args = self.timers.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_.call_args.args
        self.assertEqual(args[0], 4 * 3600 + 40 * 60)
        self.assertFalse(args[-1])

    def test_weekend_fallback_matches_saved_schedule(self):
        self.assertEqual(main.DEFAULT_STATUS_SCHEDULE, main.load_status_schedule())

    def test_text_only_boundary_is_detected(self):
        schedule = {period: [
            {"label": "Same", "emoji": ":coffee:", "status_text": "First", "start": 0, "end": 15},
            {"label": "Same", "emoji": ":coffee:", "status_text": "Second", "start": 15, "end": 0},
        ] for period in ("weekday", "weekend")}
        self.assertEqual(main.next_schedule_change_after(schedule), datetime(2026, 10, 12, 15))

    def test_preset_hold_survives_automatic_check(self):
        with patch.object(main.rumps, "Window") as window:
            window.return_value.run.return_value = SimpleNamespace(clicked=True, text="2h")
            self.app.set_preset_status_by_index(4)
        self.assertEqual(self.app.load_pause_until(), datetime(2026, 10, 12, 12, 20))
        self.app.set_slack_status.reset_mock()
        self.app.update_status()
        self.app.set_slack_status.assert_not_called()
        self.assertEqual(self.app.next_auto_update_at, datetime(2026, 10, 12, 12, 20))
        self.assertIn("Auto resumes: Work", self.app.next_change_status_text())

    def test_cancel_and_failed_preset_do_not_pause(self):
        with patch.object(main.rumps, "Window") as window:
            window.return_value.run.return_value = SimpleNamespace(clicked=False, text="2h")
            self.app.set_preset_status_by_index(4)
            self.app.set_slack_status.assert_not_called()
            window.return_value.run.return_value = SimpleNamespace(clicked=True, text="2h")
            self.app.set_slack_status.return_value = False
            self.app.set_preset_status_by_index(4)
        self.assertFalse(main.PAUSE_FILE.exists())

    def test_expired_hold_and_resume_apply_schedule(self):
        main.PAUSE_FILE.write_text((FixedDatetime.now() - timedelta(seconds=1)).isoformat())
        self.app.update_status(notify=False)
        self.app.set_slack_status.assert_called_with(":briefcase:", "At work", notify=False)
        self.assertFalse(main.PAUSE_FILE.exists())
        self.app.pause_auto_updates_until(FixedDatetime.now() + timedelta(hours=2), notify=False)
        self.app.set_slack_status.reset_mock()
        self.app.resume_auto_updates(notify=False)
        self.app.set_slack_status.assert_called_once_with(":briefcase:", "At work", notify=False)
        self.assertFalse(main.PAUSE_FILE.exists())

    def test_wake_catches_missed_deadline_and_failure_retries(self):
        self.app.next_auto_update_at = FixedDatetime.now() - timedelta(hours=1)
        self.app.check_schedule_deadline(None)
        self.app.set_slack_status.assert_called_once()
        self.app.set_slack_status.return_value = False
        self.app.update_status(notify=False)
        self.assertEqual(self.app.next_auto_update_at, FixedDatetime.now() + timedelta(seconds=60))

    def test_weather_controls_only_availability_slots_and_respects_holds(self):
        now = datetime.now(timezone.utc)
        self.app.weather_state.accept({"SPC": SourceResult("SPC", True, "SPC: Marginal", now, now + timedelta(hours=1))}, {})
        self.app.apply_weather_status_if_needed()
        self.app.set_slack_status.assert_not_called()  # Work hours.
        availability = self.app.status_schedule["weekday"][1]
        with patch.object(main, "schedule_entry_for_time", return_value=availability):
            self.app.apply_weather_status_if_needed()
            self.app.set_slack_status.assert_called_with(":tornado:", "Available for spotting", notify=False)
            self.app.set_slack_status.reset_mock()
            self.app.last_applied_status = (":tornado:", "Available for spotting")
            self.app.apply_weather_status_if_needed()
            self.app.set_slack_status.assert_not_called()  # No repeated Slack writes.
            self.app.last_applied_status = (":red_circle:", "Unavailable")
            self.app.pause_auto_updates_until(FixedDatetime.now() + timedelta(hours=1), notify=False)
            self.app.apply_weather_status_if_needed()
            self.app.set_slack_status.assert_not_called()

    def test_no_weather_data_uses_neutral_available(self):
        availability = self.app.status_schedule["weekend"][1]
        with patch.object(main, "schedule_entry_for_time", return_value=availability):
            self.app.update_status(notify=False)
        self.app.set_slack_status.assert_called_once_with(":large_green_circle:", "Available", notify=False)

    def test_completed_weather_request_does_not_override_new_manual_hold(self):
        now = datetime.now(timezone.utc)
        self.app.weather_future = Future()
        self.app.weather_future.set_result(({"SPC": SourceResult(
            "SPC", True, "SPC: Marginal", now, now + timedelta(hours=1),
        )}, {}))
        self.app.pause_auto_updates_until(FixedDatetime.now() + timedelta(hours=2), notify=False)
        with patch.object(main, "schedule_entry_for_time", return_value=self.app.status_schedule["weekday"][1]):
            self.app.poll_weather_result(None)
        self.app.set_slack_status.assert_not_called()
        self.assertTrue(self.app.weather_state.decision().spotting)
        self.assertIsNone(self.app.weather_future)

    def test_available_preset_and_menu_bar_glyph(self):
        preset = next(preset for preset in self.app.status_presets if preset["label"] == "Available")
        self.assertEqual(preset["status_text"], "Available")
        self.assertEqual(preset["emoji"], ":large_green_circle:")
        fake = SimpleNamespace(update_panel_status_icon=Mock())
        main.SlackStatusApp.update_menu_bar_icon(fake, preset["emoji"])
        self.assertEqual(fake.title, "\U0001f7e2")
        self.assertIsNone(fake.icon)
        self.assertFalse(fake.template)

    def test_schedule_summary_reflects_weather_result(self):
        now = datetime.now(timezone.utc)
        self.app.weather_state.accept({"SPC": SourceResult(
            "SPC", True, "SPC: Marginal", now, now + timedelta(hours=1),
        )}, {})
        summary = "\n".join(main.schedule_summary_lines(self.app.status_schedule, self.app.weather_state.decision()))
        self.assertIn("Spotting", summary)
        self.assertNotIn("Available", summary)

    def test_vacation_persists_and_restores_emoji_without_text(self):
        self.app.toggle_vacation_mode()
        self.assertTrue(main.load_vacation_mode())
        self.app.set_slack_status.assert_called_with(":palm_tree:", "", notify=False)
        self.assertIsNone(self.app.next_auto_update_at)
        self.app.vacation_mode = main.load_vacation_mode()
        self.app.update_status(notify=False)
        self.app.set_slack_status.assert_called_with(":palm_tree:", "", notify=False)
        self.assertEqual(self.app.get_current_default_status(), ("Vacation", ":palm_tree:"))

    def test_vacation_blocks_weather_presets_and_resume(self):
        self.app.toggle_vacation_mode()
        self.app.set_slack_status.reset_mock()
        self.app.apply_weather_status_if_needed()
        self.app.set_preset_status_by_index(0)
        self.app.set_custom_status(None)
        self.app.resume_auto_updates(notify=False)
        self.app.set_slack_status.assert_not_called()
        self.assertTrue(main.load_vacation_mode())
        self.app.update_status(notify=False, ignore_pause=True)
        self.app.set_slack_status.assert_called_once_with(":palm_tree:", "", notify=False)

    def test_turning_off_vacation_resumes_schedule_and_clears_old_pause(self):
        self.app.toggle_vacation_mode()
        main.PAUSE_FILE.write_text((FixedDatetime.now() + timedelta(days=7)).isoformat())
        self.app.set_slack_status.reset_mock()
        self.app.toggle_vacation_mode()
        self.assertFalse(main.load_vacation_mode())
        self.assertFalse(main.PAUSE_FILE.exists())
        self.app.set_slack_status.assert_called_once_with(":briefcase:", "At work", notify=False)
        self.assertIsNotNone(self.app.next_auto_update_at)

    def test_vacation_save_failure_does_not_change_status(self):
        with patch.object(main, "save_vacation_mode", side_effect=OSError("Read only")):
            self.app.toggle_vacation_mode()
        self.assertFalse(self.app.vacation_mode)
        self.app.set_slack_status.assert_not_called()

    def test_vacation_failed_slack_update_keeps_mode_and_retries(self):
        self.app.set_slack_status.return_value = False
        self.app.toggle_vacation_mode()
        self.assertTrue(main.load_vacation_mode())
        self.assertEqual(self.app.next_auto_update_at, FixedDatetime.now() + timedelta(seconds=60))

    def test_palm_tree_menu_bar_glyph(self):
        fake = SimpleNamespace(update_panel_status_icon=Mock())
        main.SlackStatusApp.update_menu_bar_icon(fake, ":palm_tree:")
        self.assertEqual(fake.title, "\U0001f334")


if __name__ == "__main__":
    unittest.main()
