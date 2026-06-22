import json
import os
import ssl
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import certifi
import objc
import rumps
from dotenv import load_dotenv
from AppKit import (
    NSApp,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSButton,
    NSFloatingWindowLevel,
    NSFont,
    NSMakeRect,
    NSPanel,
    NSTextField,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskFullSizeContentView,
    NSWindowStyleMaskHUDWindow,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject
from pip._vendor.rich._emoji_codes import EMOJI as RICH_EMOJI
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

ICON_PATH = Path(__file__).resolve().parent / "assets" / "slack_status_icon.png"
CONFIG_PATH = Path(__file__).resolve().parent / "status_schedule.json"
STATUS_ICON_PATHS = {
    ":briefcase:": Path(__file__).resolve().parent / "assets" / "status_work.png",
    "💼": Path(__file__).resolve().parent / "assets" / "status_work.png",
    ":tornado:": Path(__file__).resolve().parent / "assets" / "status_storms.png",
    "🌪": Path(__file__).resolve().parent / "assets" / "status_storms.png",
    ":sleeping:": Path(__file__).resolve().parent / "assets" / "status_sleep.png",
    "😴": Path(__file__).resolve().parent / "assets" / "status_sleep.png",
    ":coffee:": Path(__file__).resolve().parent / "assets" / "status_coffee.png",
    "☕": Path(__file__).resolve().parent / "assets" / "status_coffee.png",
}
STATUS_DISPLAY_ICONS = {
    ":briefcase:": "💼",
    "💼": "💼",
    ":tornado:": "🌪",
    "🌪": "🌪",
    ":sleeping:": "😴",
    "😴": "😴",
    ":coffee:": "☕",
    "☕": "☕",
}
DEFAULT_STATUS_SCHEDULE = {
    "weekday": [
        {"label": "Work", "emoji": ":briefcase:", "start": 4, "end": 14},
        {"label": "Storms", "emoji": ":tornado:", "start": 14, "end": 20},
        {"label": "Sleep", "emoji": ":sleeping:", "start": 20, "end": 4},
    ],
    "weekend": [
        {"label": "Coffee", "emoji": ":coffee:", "start": 4, "end": 7},
        {"label": "Storms", "emoji": ":tornado:", "start": 7, "end": 20},
        {"label": "Sleep", "emoji": ":sleeping:", "start": 20, "end": 4},
    ],
}
RUNTIME_DIR = Path(tempfile.gettempdir())
PID_FILE = RUNTIME_DIR / "slack_status_app.pid"
SIGNAL_FILE = RUNTIME_DIR / "slack_status_app.signal"
PAUSE_FILE = RUNTIME_DIR / "slack_status_app.pause_until"
EMOJI_TO_SLACK_ALIAS = {}

for alias, character in RICH_EMOJI.items():
    normalized_character = character.replace("\ufe0f", "")
    EMOJI_TO_SLACK_ALIAS.setdefault(normalized_character, f":{alias}:")


def process_is_running(pid_text):
    try:
        pid = int(pid_text.strip())
    except (TypeError, ValueError):
        return False

    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def notify_existing_instance():
    if not PID_FILE.exists():
        return False

    if not process_is_running(PID_FILE.read_text()):
        try:
            PID_FILE.unlink()
        except FileNotFoundError:
            pass
        return False

    SIGNAL_FILE.write_text(str(datetime.now().timestamp()))
    return True


def format_hour(hour):
    return f"{int(hour):02d}:00"


def normalize_custom_emoji(value):
    emoji_value = value.strip()
    if not emoji_value:
        return ""

    if emoji_value.startswith(":") and emoji_value.endswith(":"):
        return emoji_value

    normalized_value = emoji_value.replace("\ufe0f", "")
    if normalized_value in EMOJI_TO_SLACK_ALIAS:
        return EMOJI_TO_SLACK_ALIAS[normalized_value]

    if emoji_value in RICH_EMOJI:
        return f":{emoji_value}:"

    return emoji_value


def emoji_alias_names(value):
    emoji_value = value.strip()
    if not emoji_value:
        return []

    aliases = []
    candidates = [emoji_value, normalize_custom_emoji(emoji_value)]
    for candidate in candidates:
        if candidate.startswith(":") and candidate.endswith(":"):
            alias = candidate.strip(":")
            for alias_candidate in (alias, alias.replace("-", "_")):
                if alias_candidate and alias_candidate not in aliases:
                    aliases.append(alias_candidate)

    return aliases


def emoji_display_glyph(value):
    emoji_value = value.strip()
    if not emoji_value:
        return ""

    for candidate in (emoji_value, normalize_custom_emoji(emoji_value)):
        if candidate in STATUS_DISPLAY_ICONS:
            return STATUS_DISPLAY_ICONS[candidate]

        normalized_candidate = candidate.replace("\ufe0f", "")
        if normalized_candidate in EMOJI_TO_SLACK_ALIAS:
            return normalized_candidate

        for alias in emoji_alias_names(candidate):
            if alias in RICH_EMOJI:
                return RICH_EMOJI[alias].replace("\ufe0f", "")

    return ""


def status_icon_path_for_emoji(value):
    normalized_emoji = normalize_custom_emoji(value)

    for candidate in (value, normalized_emoji):
        icon_path = STATUS_ICON_PATHS.get(candidate)
        if icon_path is not None and icon_path.exists():
            return icon_path

    for alias in emoji_alias_names(value):
        icon_path = Path(__file__).resolve().parent / "assets" / f"status_{alias}.png"
        if icon_path.exists():
            return icon_path

    return None


def hour_is_in_window(hour, start, end):
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def valid_schedule_entry(entry):
    try:
        start = int(entry["start"])
        end = int(entry["end"])
    except (KeyError, TypeError, ValueError):
        return False

    return (
        isinstance(entry.get("label"), str)
        and isinstance(entry.get("emoji"), str)
        and 0 <= start <= 23
        and 0 <= end <= 23
    )


def load_status_schedule():
    if not CONFIG_PATH.exists():
        return DEFAULT_STATUS_SCHEDULE

    try:
        loaded_schedule = json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return DEFAULT_STATUS_SCHEDULE

    schedule = {}
    for period in ("weekday", "weekend"):
        entries = loaded_schedule.get(period)
        if not isinstance(entries, list) or not entries:
            return DEFAULT_STATUS_SCHEDULE
        if not all(valid_schedule_entry(entry) for entry in entries):
            return DEFAULT_STATUS_SCHEDULE
        schedule[period] = entries

    return schedule


def status_for_time(schedule, current_time=None):
    current_time = current_time or datetime.now()
    period = "weekend" if current_time.weekday() in [5, 6] else "weekday"

    for entry in schedule[period]:
        start = int(entry["start"])
        end = int(entry["end"])
        if hour_is_in_window(current_time.hour, start, end):
            return entry["label"], entry["emoji"]

    fallback = schedule[period][0]
    return fallback["label"], fallback["emoji"]


def schedule_period_for_time(current_time):
    return "weekend" if current_time.weekday() in [5, 6] else "weekday"


def next_schedule_change_after(schedule, current_time=None):
    current_time = current_time or datetime.now()
    current_period = schedule_period_for_time(current_time)
    current_label, current_emoji = status_for_time(schedule, current_time)

    for hour_offset in range(1, 24 * 8 + 1):
        candidate = current_time + timedelta(hours=hour_offset)
        candidate = candidate.replace(minute=0, second=0, microsecond=0)
        label, emoji = status_for_time(schedule, candidate)
        period = schedule_period_for_time(candidate)
        if label != current_label or emoji != current_emoji or period != current_period:
            return candidate

    return current_time + timedelta(hours=1)


def format_pause_until(pause_until):
    return pause_until.strftime("%a %b %-d, %-I:%M %p")


def parse_pause_until(value, current_time=None):
    current_time = current_time or datetime.now()
    pause_value = value.strip().lower()
    if not pause_value:
        return None

    if pause_value in {"tomorrow", "tmrw"}:
        tomorrow = current_time + timedelta(days=1)
        return tomorrow.replace(hour=8, minute=0, second=0, microsecond=0)

    if pause_value.endswith("h") and pause_value[:-1].strip().replace(".", "", 1).isdigit():
        return current_time + timedelta(hours=float(pause_value[:-1].strip()))

    if pause_value.endswith("m") and pause_value[:-1].strip().isdigit():
        return current_time + timedelta(minutes=int(pause_value[:-1].strip()))

    for date_format in ("%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p"):
        try:
            return datetime.strptime(value.strip(), date_format)
        except ValueError:
            pass

    for time_format in ("%H:%M", "%I:%M %p"):
        try:
            parsed_time = datetime.strptime(value.strip(), time_format)
        except ValueError:
            continue

        pause_until = current_time.replace(
            hour=parsed_time.hour,
            minute=parsed_time.minute,
            second=0,
            microsecond=0,
        )
        if pause_until <= current_time:
            pause_until += timedelta(days=1)
        return pause_until

    return None


def schedule_summary_lines(schedule):
    lines = []
    for title, period in (("Weekday", "weekday"), ("Weekend", "weekend")):
        parts = []
        for entry in schedule[period]:
            glyph = emoji_display_glyph(entry["emoji"])
            icon = f" {glyph}" if glyph else ""
            parts.append(
                f"{format_hour(entry['start'])}-{format_hour(entry['end'])} {entry['label']}{icon}"
            )
        lines.append(f"{title}: " + " | ".join(parts))
    return lines


class ControlPanelDelegate(NSObject):
    def initWithApp_(self, app):
        self = objc.super(ControlPanelDelegate, self).init()
        if self is None:
            return None
        self.app = app
        return self

    def triggerUpdateNow_(self, _sender):
        self.app.update_status(notify=False, ignore_pause=True)

    def triggerWorkStatus_(self, _sender):
        self.app.set_slack_status(":briefcase:", notify=False)

    def triggerRemoteStatus_(self, _sender):
        self.app.set_slack_status(":tornado:", notify=False)

    def triggerSleepStatus_(self, _sender):
        self.app.set_slack_status(":sleeping:", notify=False)

    def triggerCustomStatus_(self, _sender):
        self.app.set_custom_status(None)

    def triggerPauseAutoStatus_(self, _sender):
        self.app.pause_auto_updates_from_prompt()

    def triggerResumeAutoStatus_(self, _sender):
        self.app.resume_auto_updates()

    def triggerQuit_(self, _sender):
        self.app.quit_clicked(None)


class SlackStatusApp(rumps.App):
    def __init__(self):
        load_dotenv(Path(__file__).resolve().parent / ".env")

        icon_path = str(ICON_PATH) if ICON_PATH.exists() else None
        super(SlackStatusApp, self).__init__(
            "Slack Status",
            title="Slack" if icon_path is None else None,
            icon=icon_path,
            template=bool(icon_path),
            quit_button="Quit",
        )
        self.menu = ["Show Controls", "Update Now", "Set Custom Status", "Pause Auto Updates", "Resume Auto Updates"]

        ssl_context = ssl.create_default_context(cafile=certifi.where())
        slack_token = os.getenv("SLACK_API_TOKEN") or os.getenv("SLACK_TOKEN")
        if not slack_token:
            rumps.alert(
                title="Slack Status - Missing Token",
                message="Set SLACK_API_TOKEN in .env or your shell environment.",
            )
        self.client = WebClient(
            token=slack_token,
            ssl=ssl_context,
        )
        self.status_schedule = load_status_schedule()

        self.control_panel = None
        self.control_panel_delegate = None
        self.default_status_button = None
        self.panel_current_status_label = None
        self.panel_pause_status_label = None
        self.panel_schedule_label = None
        self.panel_status_icon_label = None
        self.panel_status_label = None
        self.last_signal_mtime = SIGNAL_FILE.stat().st_mtime if SIGNAL_FILE.exists() else 0.0
        PID_FILE.write_text(str(os.getpid()))

        # Delay startup UI until after the menu bar item is visible.
        self.startup_timer = rumps.Timer(self.run_startup_update, 1)
        self.startup_timer.start()
        self.timer = rumps.Timer(self.update_status, 3600)
        self.timer.start()
        self.signal_timer = rumps.Timer(self.check_for_show_panel_signal, 0.25)
        self.signal_timer.start()

    def run(self, **options):
        try:
            super(SlackStatusApp, self).run(**options)
        finally:
            self.cleanup_runtime_files()

    def cleanup_runtime_files(self):
        if PID_FILE.exists():
            try:
                if PID_FILE.read_text().strip() == str(os.getpid()):
                    PID_FILE.unlink()
            except FileNotFoundError:
                pass

    def quit_clicked(self, _):
        self.cleanup_runtime_files()
        rumps.quit_application()

    def get_current_default_status(self):
        return status_for_time(self.status_schedule)

    def build_control_panel(self):
        if self.control_panel is not None:
            return

        self.control_panel_delegate = ControlPanelDelegate.alloc().initWithApp_(self)

        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 420, 430),
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskHUDWindow
            | NSWindowStyleMaskFullSizeContentView,
            NSBackingStoreBuffered,
            False,
        )
        panel.setTitle_("Slack Status Controls")
        panel.setFloatingPanel_(True)
        panel.setLevel_(NSFloatingWindowLevel)
        panel.setReleasedWhenClosed_(False)
        panel.setHidesOnDeactivate_(False)
        panel.center()

        content_view = panel.contentView()

        status_icon_label = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 364, 38, 42))
        status_icon_label.setStringValue_(self.get_status_display_icon(self.get_current_default_status()[1]))
        status_icon_label.setBezeled_(False)
        status_icon_label.setDrawsBackground_(False)
        status_icon_label.setEditable_(False)
        status_icon_label.setSelectable_(False)
        status_icon_label.setFont_(NSFont.systemFontOfSize_(30))
        content_view.addSubview_(status_icon_label)
        self.panel_status_icon_label = status_icon_label

        title_label = NSTextField.alloc().initWithFrame_(NSMakeRect(68, 385, 332, 24))
        title_label.setStringValue_("Quick actions")
        title_label.setBezeled_(False)
        title_label.setDrawsBackground_(False)
        title_label.setEditable_(False)
        title_label.setSelectable_(False)
        title_label.setFont_(NSFont.boldSystemFontOfSize_(16))
        content_view.addSubview_(title_label)

        subtitle_label = NSTextField.alloc().initWithFrame_(NSMakeRect(68, 365, 332, 18))
        subtitle_label.setStringValue_("Launch the script again anytime to reopen this panel.")
        subtitle_label.setBezeled_(False)
        subtitle_label.setDrawsBackground_(False)
        subtitle_label.setEditable_(False)
        subtitle_label.setSelectable_(False)
        subtitle_label.setFont_(NSFont.systemFontOfSize_(12))
        content_view.addSubview_(subtitle_label)

        schedule_title_label = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 312, 380, 20))
        schedule_title_label.setStringValue_("Schedule")
        schedule_title_label.setBezeled_(False)
        schedule_title_label.setDrawsBackground_(False)
        schedule_title_label.setEditable_(False)
        schedule_title_label.setSelectable_(False)
        schedule_title_label.setFont_(NSFont.boldSystemFontOfSize_(13))
        content_view.addSubview_(schedule_title_label)

        schedule_label = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 255, 380, 50))
        schedule_label.setStringValue_("\n".join(schedule_summary_lines(self.status_schedule)))
        schedule_label.setBezeled_(False)
        schedule_label.setDrawsBackground_(False)
        schedule_label.setEditable_(False)
        schedule_label.setSelectable_(False)
        schedule_label.setFont_(NSFont.systemFontOfSize_(12))
        content_view.addSubview_(schedule_label)
        self.panel_schedule_label = schedule_label

        pause_status_label = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 225, 380, 18))
        pause_status_label.setStringValue_(self.auto_pause_status_text())
        pause_status_label.setBezeled_(False)
        pause_status_label.setDrawsBackground_(False)
        pause_status_label.setEditable_(False)
        pause_status_label.setSelectable_(False)
        pause_status_label.setFont_(NSFont.systemFontOfSize_(12))
        content_view.addSubview_(pause_status_label)
        self.panel_pause_status_label = pause_status_label

        current_status_label = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 42, 380, 18))
        current_status_label.setStringValue_("Current Slack: checking...")
        current_status_label.setBezeled_(False)
        current_status_label.setDrawsBackground_(False)
        current_status_label.setEditable_(False)
        current_status_label.setSelectable_(False)
        current_status_label.setFont_(NSFont.systemFontOfSize_(12))
        content_view.addSubview_(current_status_label)
        self.panel_current_status_label = current_status_label

        status_label = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 18, 380, 18))
        status_label.setStringValue_("Ready")
        status_label.setBezeled_(False)
        status_label.setDrawsBackground_(False)
        status_label.setEditable_(False)
        status_label.setSelectable_(False)
        status_label.setFont_(NSFont.systemFontOfSize_(12))
        content_view.addSubview_(status_label)
        self.panel_status_label = status_label

        default_label, _ = self.get_current_default_status()
        buttons = [
            ("Custom...", "triggerCustomStatus:", 20, 175, 120),
            (f"Default: {default_label}", "triggerUpdateNow:", 150, 175, 120),
            ("Pause Auto...", "triggerPauseAutoStatus:", 280, 175, 120),
            ("Storms", "triggerRemoteStatus:", 20, 130, 120),
            ("Sleep", "triggerSleepStatus:", 150, 130, 120),
            ("Resume Auto", "triggerResumeAutoStatus:", 280, 130, 120),
            ("Work", "triggerWorkStatus:", 20, 85, 120),
            ("Quit", "triggerQuit:", 280, 85, 120),
        ]

        for title, action_name, x_pos, y_pos, width in buttons:
            button = NSButton.alloc().initWithFrame_(NSMakeRect(x_pos, y_pos, width, 32))
            button.setTitle_(title)
            button.setBezelStyle_(NSBezelStyleRounded)
            button.setTarget_(self.control_panel_delegate)
            button.setAction_(action_name)
            content_view.addSubview_(button)
            if action_name == "triggerUpdateNow:":
                self.default_status_button = button

        self.control_panel = panel

    def show_control_panel(self):
        self.build_control_panel()
        self.refresh_default_status_button()
        NSApp().activateIgnoringOtherApps_(True)
        self.control_panel.center()
        self.control_panel.makeKeyAndOrderFront_(None)

    def hide_control_panel(self):
        if self.control_panel is not None:
            self.control_panel.orderOut_(None)

    def update_panel_status(self, message):
        if self.panel_status_label is not None:
            self.panel_status_label.setStringValue_(message)

    def update_panel_current_status(self, message):
        if self.panel_current_status_label is not None:
            self.panel_current_status_label.setStringValue_(message)

    def update_panel_pause_status(self):
        if self.panel_pause_status_label is not None:
            self.panel_pause_status_label.setStringValue_(self.auto_pause_status_text())

    def load_pause_until(self):
        if not PAUSE_FILE.exists():
            return None

        try:
            pause_until = datetime.fromisoformat(PAUSE_FILE.read_text().strip())
        except (OSError, ValueError):
            return None

        if pause_until <= datetime.now():
            self.resume_auto_updates(notify=False)
            return None

        return pause_until

    def auto_pause_status_text(self):
        pause_until = self.load_pause_until()
        if pause_until is None:
            return "Auto updates: active"
        return f"Auto updates: paused until {format_pause_until(pause_until)}"

    def auto_updates_are_paused(self):
        return self.load_pause_until() is not None

    def pause_auto_updates_until(self, pause_until, notify=True):
        PAUSE_FILE.write_text(pause_until.isoformat(timespec="minutes"))
        message = f"Auto updates paused until {format_pause_until(pause_until)}"
        self.update_panel_pause_status()
        self.update_panel_status(message)
        if notify:
            rumps.notification(
                title="Slack Status Auto Updates Paused",
                subtitle="",
                message=message,
                sound="Ping",
            )

    def pause_auto_updates_from_prompt(self, _=None):
        default_until = next_schedule_change_after(self.status_schedule)
        response = rumps.Window(
            title="Pause Slack Status Auto Updates",
            message="Pause until when? Use 2h, 90m, tomorrow, 18:30, or YYYY-MM-DD HH:MM.",
            default_text=default_until.strftime("%Y-%m-%d %H:%M"),
        ).run()

        if not response.clicked:
            return

        pause_until = parse_pause_until(response.text)
        if pause_until is None or pause_until <= datetime.now():
            rumps.alert(
                title="Invalid Pause Time",
                message="Enter a future time like 2h, 90m, tomorrow, 18:30, or 2026-06-21 18:30.",
            )
            return

        self.pause_auto_updates_until(pause_until)

    def resume_auto_updates(self, _=None, notify=True):
        try:
            PAUSE_FILE.unlink()
        except FileNotFoundError:
            pass

        self.update_panel_pause_status()
        self.update_panel_status("Auto updates active")
        if notify:
            rumps.notification(
                title="Slack Status Auto Updates Resumed",
                subtitle="",
                message="Scheduled status updates are active again.",
                sound="Ping",
            )

    def get_status_display_icon(self, emoji):
        return emoji_display_glyph(emoji) or "●"

    def update_panel_status_icon(self, emoji):
        if self.panel_status_icon_label is not None:
            self.panel_status_icon_label.setStringValue_(self.get_status_display_icon(emoji))

    def refresh_default_status_button(self):
        if self.default_status_button is None:
            return
        default_label, _ = self.get_current_default_status()
        self.default_status_button.setTitle_(f"Default: {default_label}")
        self.update_panel_pause_status()

    def update_menu_bar_icon(self, emoji):
        icon_path = status_icon_path_for_emoji(emoji)
        if icon_path is not None:
            self.update_panel_status_icon(emoji)
            self.template = True
            self.icon = str(icon_path)
            self.title = None
            return

        display_glyph = emoji_display_glyph(emoji)
        if display_glyph:
            self.update_panel_status_icon(emoji)
            self.icon = None
            self.template = False
            self.title = display_glyph
            return

        self.update_panel_status_icon(emoji)
        if ICON_PATH.exists():
            self.template = True
            self.icon = str(ICON_PATH)
            self.title = None
        else:
            self.icon = None
            self.title = "Slack"

    def check_for_show_panel_signal(self, _):
        if not SIGNAL_FILE.exists():
            return

        signal_mtime = SIGNAL_FILE.stat().st_mtime
        if signal_mtime <= self.last_signal_mtime:
            return

        self.last_signal_mtime = signal_mtime
        self.show_control_panel()

    def run_startup_update(self, _):
        self.startup_timer.stop()
        self.show_control_panel()
        self.refresh_current_slack_status()
        self.update_status()

    @rumps.clicked("Show Controls")
    def show_controls(self, _):
        self.show_control_panel()

    @rumps.clicked("Update Now")
    def update_now(self, _):
        self.update_status(ignore_pause=True)

    @rumps.clicked("Set Custom Status")
    def set_custom_status(self, _):
        response = rumps.Window(
            title="Custom Slack Status",
            message="Enter status emoji and optional text separated by a comma\nAuto updates pause until the next schedule change.",
            default_text="🌪, Working remotely",
        ).run()

        if response.clicked:
            user_input = response.text.strip()
            parts = [part.strip() for part in user_input.split(",", 1)]
            if len(parts) == 2:
                emoji, status_text = parts
                if self.set_slack_status(emoji, status_text, notify=False):
                    self.pause_auto_updates_until(next_schedule_change_after(self.status_schedule), notify=False)
            elif len(parts) == 1 and parts[0]:
                emoji = parts[0]
                if self.set_slack_status(emoji, notify=False):
                    self.pause_auto_updates_until(next_schedule_change_after(self.status_schedule), notify=False)

    @rumps.clicked("Pause Auto Updates")
    def pause_auto_updates_clicked(self, _):
        self.pause_auto_updates_from_prompt()

    @rumps.clicked("Resume Auto Updates")
    def resume_auto_updates_clicked(self, _):
        self.resume_auto_updates()

    def update_status(self, _=None, notify=True, ignore_pause=False):
        self.refresh_default_status_button()
        if not ignore_pause and self.auto_updates_are_paused():
            self.update_panel_status(self.auto_pause_status_text())
            return

        _, emoji = self.get_current_default_status()
        self.set_slack_status(emoji, notify=notify)

    def refresh_current_slack_status(self):
        try:
            response = self.client.users_profile_get()
            profile = response.get("profile", {})
            status_emoji = profile.get("status_emoji") or ""
            status_text = profile.get("status_text") or ""
            if status_emoji or status_text:
                self.update_panel_current_status(f"Current Slack: {status_emoji} {status_text}".strip())
                if status_emoji:
                    self.update_menu_bar_icon(status_emoji)
            else:
                self.update_panel_current_status("Current Slack: no status set")
        except SlackApiError as e:
            self.update_panel_current_status(f"Current Slack: error ({e.response['error']})")
        except Exception as e:
            self.update_panel_current_status(f"Current Slack: error ({e})")

    def set_slack_status(self, emoji, status_text="", notify=True):
        requested_emoji = emoji.strip()
        normalized_emoji = normalize_custom_emoji(requested_emoji)
        emoji_candidates = []

        for candidate in (requested_emoji, normalized_emoji):
            if candidate and candidate not in emoji_candidates:
                emoji_candidates.append(candidate)

        try:
            last_error = None
            applied_emoji = requested_emoji or normalized_emoji

            for candidate in emoji_candidates:
                try:
                    self.client.users_profile_set(
                        profile={
                            "status_text": status_text,
                            "status_emoji": candidate,
                            "status_expiration": 0,
                        }
                    )
                    applied_emoji = candidate
                    last_error = None
                    break
                except SlackApiError as error:
                    last_error = error
                    if error.response["error"] != "profile_status_set_failed_not_valid_emoji":
                        raise

            if last_error is not None:
                raise last_error

            # Notification with sound depending on emoji
            if applied_emoji in {":tornado:", "🌪"}:
                sound_name = "Funk"
            elif applied_emoji in {":briefcase:", "💼"}:
                sound_name = "Ping"
            elif applied_emoji in {":sleeping:", "😴"}:
                sound_name = "Basso"
            else:
                sound_name = "Submarine"

            status_message = f"Status set to {applied_emoji} {status_text}".strip()
            self.update_menu_bar_icon(applied_emoji)
            self.update_panel_current_status(f"Current Slack: {applied_emoji} {status_text}".strip())
            self.update_panel_status(status_message)

            if notify:
                rumps.notification(
                    title="Slack Status Updated",
                    subtitle="",
                    message=status_message,
                    sound=sound_name,
                )
            return True
        except SlackApiError as e:
            self.update_panel_status(f"Slack error: {e.response['error']}")
            rumps.alert(title="Error", message=f"Failed to update status: {e.response['error']}")
            return False
        except Exception as e:
            self.update_panel_status(f"Error: {e}")
            rumps.alert(title="Error", message=f"Failed to update status: {e}")
            return False


if __name__ == "__main__":
    if notify_existing_instance():
        raise SystemExit(0)

    app = SlackStatusApp()
    app.run()
