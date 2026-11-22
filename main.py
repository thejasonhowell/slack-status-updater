import json
import os
import ssl
import tempfile
from concurrent.futures import ThreadPoolExecutor
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
    NSColor,
    NSFloatingWindowLevel,
    NSFont,
    NSMakeRect,
    NSPanel,
    NSSwitchButton,
    NSTextField,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskFullSizeContentView,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject, NSTimer
from pip._vendor.rich._emoji_codes import EMOJI as RICH_EMOJI
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from weather_risk import REFRESH_INTERVAL, WeatherService, WeatherState, utc_now

ICON_PATH = Path(__file__).resolve().parent / "assets" / "slack_status_icon.png"
CONFIG_PATH = Path(__file__).resolve().parent / "status_schedule.json"
PRESETS_PATH = Path(__file__).resolve().parent / "status_presets.json"
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
    ":large_green_circle:": "🟢",
    ":palm_tree:": "🌴",
}
DEFAULT_STATUS_SCHEDULE = {
    "weekday": [
        {"label": "Work", "emoji": ":briefcase:", "status_text": "At work", "start": 7, "end": 15},
        {"label": "Storms", "emoji": ":tornado:", "status_text": "Available for spotting", "weather_aware": True, "start": 15, "end": 21},
        {"label": "Sleep", "emoji": ":sleeping:", "status_text": "Sleeping", "start": 21, "end": 7},
    ],
    "weekend": [
        {"label": "Coffee", "emoji": ":coffee:", "start": 6, "end": 8},
        {"label": "Storms", "emoji": ":tornado:", "weather_aware": True, "start": 8, "end": 21},
        {"label": "Sleep", "emoji": ":sleeping:", "status_text": "Sleeping", "start": 21, "end": 6},
    ],
}
DEFAULT_STATUS_PRESETS = [
    {"label": "Work", "emoji": ":briefcase:", "status_text": "At work"},
    {"label": "Storms", "emoji": ":tornado:", "status_text": "Available for spotting"},
    {"label": "Sleep", "emoji": ":sleeping:", "status_text": "Sleeping"},
    {"label": "Coffee", "emoji": ":coffee:", "status_text": ""},
    {"label": "Available", "emoji": ":large_green_circle:", "status_text": "Available"},
]
RUNTIME_DIR = Path(tempfile.gettempdir())
PID_FILE = RUNTIME_DIR / "slack_status_app.pid"
SIGNAL_FILE = RUNTIME_DIR / "slack_status_app.signal"
PAUSE_FILE = RUNTIME_DIR / "slack_status_app.pause_until"
VACATION_FILE = Path.home() / "Library" / "Application Support" / "Slack Status Updater" / "vacation.json"
EMOJI_TO_SLACK_ALIAS = {}

for alias, character in RICH_EMOJI.items():
    normalized_character = character.replace("\ufe0f", "")
    EMOJI_TO_SLACK_ALIAS.setdefault(normalized_character, f":{alias}:")


def load_vacation_mode():
    try:
        return json.loads(VACATION_FILE.read_text()).get("enabled") is True
    except (OSError, ValueError, AttributeError):
        return False


def save_vacation_mode(enabled):
    if enabled:
        VACATION_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = VACATION_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps({"enabled": True}))
        temporary.replace(VACATION_FILE)
    else:
        VACATION_FILE.unlink(missing_ok=True)


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
        and isinstance(entry.get("status_text", ""), str)
        and isinstance(entry.get("weather_aware", False), bool)
        and 0 <= start <= 23
        and 0 <= end <= 23
    )


def valid_preset_entry(entry):
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("label"), str)
        and bool(entry["label"].strip())
        and isinstance(entry.get("emoji"), str)
        and bool(entry["emoji"].strip())
        and isinstance(entry.get("status_text", ""), str)
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


def load_status_presets():
    if not PRESETS_PATH.exists():
        return DEFAULT_STATUS_PRESETS

    try:
        loaded_presets = json.loads(PRESETS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return DEFAULT_STATUS_PRESETS

    if not isinstance(loaded_presets, list) or not loaded_presets:
        return DEFAULT_STATUS_PRESETS

    presets = []
    for entry in loaded_presets:
        if not valid_preset_entry(entry):
            return DEFAULT_STATUS_PRESETS
        presets.append(
            {
                "label": entry["label"].strip(),
                "emoji": entry["emoji"].strip(),
                "status_text": entry.get("status_text", "").strip(),
            }
        )

    return presets


def schedule_entry_for_time(schedule, current_time=None):
    current_time = current_time or datetime.now()
    period = "weekend" if current_time.weekday() in [5, 6] else "weekday"

    for entry in schedule[period]:
        start = int(entry["start"])
        end = int(entry["end"])
        if hour_is_in_window(current_time.hour, start, end):
            return entry

    return schedule[period][0]


def status_for_time(schedule, current_time=None):
    entry = schedule_entry_for_time(schedule, current_time)
    return entry["label"], entry["emoji"]


def schedule_period_for_time(current_time):
    return "weekend" if current_time.weekday() in [5, 6] else "weekday"


def next_schedule_change_after(schedule, current_time=None):
    current_time = current_time or datetime.now()
    current_entry = schedule_entry_for_time(schedule, current_time)

    for hour_offset in range(1, 24 * 8 + 1):
        candidate = current_time + timedelta(hours=hour_offset)
        candidate = candidate.replace(minute=0, second=0, microsecond=0)
        entry = schedule_entry_for_time(schedule, candidate)
        if entry != current_entry:
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


def schedule_summary_lines(schedule, weather_decision=None):
    lines = []
    for title, period in (("Weekday", "weekday"), ("Weekend", "weekend")):
        parts = []
        for entry in schedule[period]:
            label = "Available" if entry.get("weather_aware") else entry["label"]
            glyph = emoji_display_glyph(":large_green_circle:" if entry.get("weather_aware") else entry["emoji"])
            if entry.get("weather_aware") and weather_decision is not None and weather_decision.spotting:
                label, glyph = "Spotting", emoji_display_glyph(":tornado:")
            icon = f" {glyph}" if glyph else ""
            parts.append(
                f"{format_hour(entry['start'])}-{format_hour(entry['end'])} {label}{icon}"
            )
        lines.append(f"{title}: " + " | ".join(parts))
    return lines


def preset_button_title(preset):
    glyph = emoji_display_glyph(preset["emoji"])
    title = f"{glyph} {preset['label']}" if glyph else preset["label"]
    if len(title) > 18:
        return title[:15] + "..."
    return title


class ControlPanelDelegate(NSObject):
    def initWithApp_(self, app):
        self = objc.super(ControlPanelDelegate, self).init()
        if self is None:
            return None
        self.app = app
        return self

    def triggerUpdateNow_(self, _sender):
        self.app.update_status(notify=False, ignore_pause=True)

    def triggerPresetStatus_(self, sender):
        self.app.set_preset_status_by_index(sender.tag())

    def triggerCustomStatus_(self, _sender):
        self.app.set_custom_status(None)

    def triggerPauseAutoStatus_(self, _sender):
        self.app.pause_auto_updates_from_prompt()

    def triggerResumeAutoStatus_(self, _sender):
        self.app.resume_auto_updates()

    def triggerQuit_(self, _sender):
        self.app.quit_clicked(None)

    def triggerScheduledUpdate_(self, _sender):
        self.app.run_scheduled_update(None)

    def triggerVacationMode_(self, _sender):
        self.app.toggle_vacation_mode()


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
        self.menu = ["Show Controls", "Vacation Mode", "Update Now", "Set Custom Status", "Pause Auto Updates", "Resume Auto Updates"]

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
        self.status_presets = load_status_presets()
        self.vacation_mode = load_vacation_mode()
        self.panel_vacation_toggle = None
        self.weather_state = WeatherState(RUNTIME_DIR / "slack_status_weather.json")
        self.weather_service = WeatherService()
        self.weather_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="weather")
        self.weather_future = None
        self.next_weather_check_at = None
        self.last_applied_status = None
        self.panel_weather_label = None

        self.control_panel = None
        self.control_panel_delegate = None
        self.default_status_button = None
        self.panel_current_status_label = None
        self.panel_pause_status_label = None
        self.panel_next_change_label = None
        self.panel_schedule_label = None
        self.panel_status_icon_label = None
        self.panel_status_label = None
        self.refresh_vacation_controls()
        self.last_signal_mtime = SIGNAL_FILE.stat().st_mtime if SIGNAL_FILE.exists() else 0.0
        PID_FILE.write_text(str(os.getpid()))

        # Delay startup UI until after the menu bar item is visible.
        self.startup_timer = rumps.Timer(self.run_startup_update, 1)
        self.startup_timer.start()
        self.timer = None
        self.schedule_timer_delegate = ControlPanelDelegate.alloc().initWithApp_(self)
        self.next_auto_update_at = None
        self.schedule_watchdog = rumps.Timer(self.check_schedule_deadline, 30)
        self.schedule_watchdog.start()
        self.signal_timer = rumps.Timer(self.check_for_show_panel_signal, 0.25)
        self.signal_timer.start()
        self.weather_poll_timer = rumps.Timer(self.poll_weather_result, 1)
        self.weather_poll_timer.start()

    def run(self, **options):
        try:
            super(SlackStatusApp, self).run(**options)
        finally:
            self.cleanup_runtime_files()

    def cleanup_runtime_files(self):
        if self.timer is not None:
            self.timer.invalidate()
        self.weather_executor.shutdown(wait=False, cancel_futures=True)
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
        if self.vacation_mode:
            return "Vacation", ":palm_tree:"
        entry = schedule_entry_for_time(self.status_schedule)
        if entry.get("weather_aware"):
            decision = self.weather_state.decision()
            return ("Spotting" if decision.spotting else "Available"), decision.status[0]
        return entry["label"], entry["emoji"]

    def build_control_panel(self):
        if self.control_panel is not None:
            return

        self.control_panel_delegate = ControlPanelDelegate.alloc().initWithApp_(self)
        button_width = 120
        utility_button_width = 90
        button_height = 32
        button_gap = 10
        row_spacing = 45
        bottom_status_height = 22
        bottom_action_y = 100
        preset_rows = max(1, (len(self.status_presets) + 2) // 3)
        extra_panel_height = max(0, preset_rows - 2) * row_spacing
        panel_height = 581 + extra_panel_height
        controls_y = bottom_action_y + row_spacing * (preset_rows + 1)
        pause_status_y = controls_y + 112
        schedule_y = pause_status_y + 30
        schedule_title_y = schedule_y + 73
        header_icon_y = panel_height - 88
        header_title_y = panel_height - 67
        header_subtitle_y = panel_height - 88

        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 420, panel_height),
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskFullSizeContentView,
            NSBackingStoreBuffered,
            False,
        )
        panel.setTitle_("Slack Status Controls")
        panel.setOpaque_(True)
        panel.setBackgroundColor_(NSColor.windowBackgroundColor())
        panel.setFloatingPanel_(True)
        panel.setLevel_(NSFloatingWindowLevel)
        panel.setReleasedWhenClosed_(False)
        panel.setHidesOnDeactivate_(False)
        panel.center()

        content_view = panel.contentView()

        status_icon_label = NSTextField.alloc().initWithFrame_(NSMakeRect(20, header_icon_y, 38, 42))
        status_icon_label.setStringValue_(self.get_status_display_icon(self.get_current_default_status()[1]))
        status_icon_label.setBezeled_(False)
        status_icon_label.setDrawsBackground_(False)
        status_icon_label.setEditable_(False)
        status_icon_label.setSelectable_(False)
        status_icon_label.setFont_(NSFont.systemFontOfSize_(30))
        content_view.addSubview_(status_icon_label)
        self.panel_status_icon_label = status_icon_label

        title_label = NSTextField.alloc().initWithFrame_(NSMakeRect(68, header_title_y, 332, 24))
        title_label.setStringValue_("Quick actions")
        title_label.setBezeled_(False)
        title_label.setDrawsBackground_(False)
        title_label.setEditable_(False)
        title_label.setSelectable_(False)
        title_label.setFont_(NSFont.boldSystemFontOfSize_(16))
        content_view.addSubview_(title_label)

        subtitle_label = NSTextField.alloc().initWithFrame_(NSMakeRect(68, header_subtitle_y, 332, 18))
        subtitle_label.setStringValue_("Launch the script again anytime to reopen this panel.")
        subtitle_label.setBezeled_(False)
        subtitle_label.setDrawsBackground_(False)
        subtitle_label.setEditable_(False)
        subtitle_label.setSelectable_(False)
        subtitle_label.setFont_(NSFont.systemFontOfSize_(12))
        content_view.addSubview_(subtitle_label)

        schedule_title_label = NSTextField.alloc().initWithFrame_(NSMakeRect(20, schedule_title_y, 380, 20))
        schedule_title_label.setStringValue_("Schedule")
        schedule_title_label.setBezeled_(False)
        schedule_title_label.setDrawsBackground_(False)
        schedule_title_label.setEditable_(False)
        schedule_title_label.setSelectable_(False)
        schedule_title_label.setFont_(NSFont.boldSystemFontOfSize_(13))
        content_view.addSubview_(schedule_title_label)

        schedule_label = NSTextField.alloc().initWithFrame_(NSMakeRect(20, schedule_y, 380, 66))
        schedule_label.setStringValue_("\n".join(schedule_summary_lines(self.status_schedule, self.weather_state.decision())))
        schedule_label.setBezeled_(False)
        schedule_label.setDrawsBackground_(False)
        schedule_label.setEditable_(False)
        schedule_label.setSelectable_(False)
        schedule_label.setFont_(NSFont.systemFontOfSize_(12))
        content_view.addSubview_(schedule_label)
        self.panel_schedule_label = schedule_label

        pause_status_label = NSTextField.alloc().initWithFrame_(NSMakeRect(20, pause_status_y, 380, 18))
        pause_status_label.setStringValue_(self.auto_pause_status_text())
        pause_status_label.setBezeled_(False)
        pause_status_label.setDrawsBackground_(False)
        pause_status_label.setEditable_(False)
        pause_status_label.setSelectable_(False)
        pause_status_label.setFont_(NSFont.systemFontOfSize_(12))
        content_view.addSubview_(pause_status_label)
        self.panel_pause_status_label = pause_status_label

        next_change_label = NSTextField.alloc().initWithFrame_(NSMakeRect(20, pause_status_y - 22, 380, 20))
        next_change_label.setStringValue_(self.next_change_status_text())
        next_change_label.setBezeled_(False)
        next_change_label.setDrawsBackground_(False)
        next_change_label.setEditable_(False)
        next_change_label.setSelectable_(False)
        next_change_label.setFont_(NSFont.systemFontOfSize_(12))
        content_view.addSubview_(next_change_label)
        self.panel_next_change_label = next_change_label

        weather_label = NSTextField.alloc().initWithFrame_(NSMakeRect(20, controls_y + 42, 380, 42))
        weather_label.setBezeled_(False)
        weather_label.setDrawsBackground_(False)
        weather_label.setEditable_(False)
        weather_label.setSelectable_(False)
        weather_label.setFont_(NSFont.systemFontOfSize_(12))
        content_view.addSubview_(weather_label)
        self.panel_weather_label = weather_label
        self.update_weather_panel()

        current_status_label = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 42, 380, bottom_status_height))
        current_status_label.setStringValue_("Current Slack: checking...")
        current_status_label.setBezeled_(False)
        current_status_label.setDrawsBackground_(False)
        current_status_label.setEditable_(False)
        current_status_label.setSelectable_(False)
        current_status_label.setFont_(NSFont.systemFontOfSize_(12))
        content_view.addSubview_(current_status_label)
        self.panel_current_status_label = current_status_label

        status_label = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 18, 380, bottom_status_height))
        status_label.setStringValue_("Ready")
        status_label.setBezeled_(False)
        status_label.setDrawsBackground_(False)
        status_label.setEditable_(False)
        status_label.setSelectable_(False)
        status_label.setFont_(NSFont.systemFontOfSize_(12))
        content_view.addSubview_(status_label)
        self.panel_status_label = status_label

        vacation_toggle = NSButton.alloc().initWithFrame_(NSMakeRect(20, bottom_action_y, 240, button_height))
        vacation_toggle.setButtonType_(NSSwitchButton)
        vacation_toggle.setTitle_("🌴 Vacation mode")
        vacation_toggle.setState_(int(self.vacation_mode))
        vacation_toggle.setTarget_(self.control_panel_delegate)
        vacation_toggle.setAction_("triggerVacationMode:")
        content_view.addSubview_(vacation_toggle)
        self.panel_vacation_toggle = vacation_toggle

        buttons = [
            ("Custom...", "triggerCustomStatus:", 20, controls_y, utility_button_width, None),
            ("Default", "triggerUpdateNow:", 115, controls_y, utility_button_width, None),
            ("Pause Auto...", "triggerPauseAutoStatus:", 210, controls_y, utility_button_width, None),
            ("Resume Auto", "triggerResumeAutoStatus:", 305, controls_y, utility_button_width, None),
        ]

        for index, preset in enumerate(self.status_presets):
            row = index // 3
            column = index % 3
            x_pos = 20 + column * (button_width + button_gap)
            y_pos = bottom_action_y + row_spacing * (preset_rows - row)
            buttons.append((preset_button_title(preset), "triggerPresetStatus:", x_pos, y_pos, button_width, index))

        buttons.extend(
            [
                ("Quit", "triggerQuit:", 280, bottom_action_y, button_width, None),
            ]
        )

        for title, action_name, x_pos, y_pos, width, tag in buttons:
            button = NSButton.alloc().initWithFrame_(NSMakeRect(x_pos, y_pos, width, button_height))
            button.setTitle_(title)
            button.setBezelStyle_(NSBezelStyleRounded)
            button.setTarget_(self.control_panel_delegate)
            button.setAction_(action_name)
            if tag is not None:
                button.setTag_(tag)
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
        if self.panel_next_change_label is not None:
            self.panel_next_change_label.setStringValue_(self.next_change_status_text())

    def next_change_status_text(self):
        if self.vacation_mode:
            return "Until Vacation mode is turned off"
        pause_until = self.load_pause_until()
        if pause_until is not None:
            entry = schedule_entry_for_time(self.status_schedule, pause_until)
            label = "Availability" if entry.get("weather_aware") else entry["label"]
            return f"Auto resumes: {label} at {format_pause_until(pause_until)}"
        next_change = next_schedule_change_after(self.status_schedule)
        entry = schedule_entry_for_time(self.status_schedule, next_change)
        label = "Availability" if entry.get("weather_aware") else entry["label"]
        return f"Next: {label} at {format_pause_until(next_change)}"

    def start_weather_refresh(self, force=False):
        if self.weather_future is not None:
            return
        now = utc_now()
        if not force and self.next_weather_check_at is not None and now < self.next_weather_check_at:
            return
        self.next_weather_check_at = now + REFRESH_INTERVAL
        self.weather_future = self.weather_executor.submit(self.weather_service.fetch)
        self.update_weather_panel()

    def poll_weather_result(self, _):
        if self.weather_future is None or not self.weather_future.done():
            return
        try:
            results, errors = self.weather_future.result()
        except Exception as error:
            results, errors = {}, {source: str(error) for source in ("SPC", "WPC", "NWS")}
        self.weather_future = None
        self.weather_state.accept(results, errors)
        self.update_weather_panel()
        self.refresh_default_status_button()
        self.apply_weather_status_if_needed()

    def update_weather_panel(self):
        if self.panel_weather_label is None:
            return
        decision = self.weather_state.decision()
        if self.panel_schedule_label is not None:
            self.panel_schedule_label.setStringValue_("\n".join(schedule_summary_lines(self.status_schedule, decision)))
        detail = "; ".join(decision.reasons) if decision.spotting else "No qualifying severe/flood risk"
        if decision.unavailable:
            detail = "Weather unavailable: " + ", ".join(decision.unavailable)
            if decision.spotting:
                detail = "Risk present; unavailable: " + ", ".join(decision.unavailable)
        elif decision.cached:
            detail += " (cached)"
        if self.weather_future is not None:
            detail = "Checking weather..." if decision.checked_at is None else detail
        checked = decision.checked_at.astimezone().strftime("%-I:%M %p") if decision.checked_at else "not yet checked"
        if len(detail) > 48:
            detail = detail[:45] + "..."
        self.panel_weather_label.setStringValue_(f"Weather: {detail}\nTazewell County, IL | Checked: {checked}")
        self.panel_weather_label.setToolTip_("\n".join(
            list(decision.reasons) + [f"{source}: {error}" for source, error in self.weather_state.errors.items()]
        ))

    def apply_weather_status_if_needed(self):
        if self.vacation_mode:
            return
        entry = schedule_entry_for_time(self.status_schedule)
        if not entry.get("weather_aware") or self.auto_updates_are_paused():
            return
        retry_until = getattr(self, "weather_status_retry_until", None)
        if retry_until is not None and datetime.now() < retry_until:
            return
        if self.weather_state.decision().status != self.last_applied_status:
            self.update_status(notify=False, refresh_weather=False)

    def schedule_next_auto_update(self, retry=False):
        if self.timer is not None:
            self.timer.invalidate()
            self.timer = None
        if self.vacation_mode and not retry:
            self.next_auto_update_at = None
            self.update_panel_pause_status()
            return
        now = datetime.now()
        deadline = self.load_pause_until() or next_schedule_change_after(self.status_schedule, now)
        if retry:
            deadline = now + timedelta(seconds=60)
        self.next_auto_update_at = deadline
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            max(0.1, (deadline - now).total_seconds()),
            self.schedule_timer_delegate, "triggerScheduledUpdate:", None, False,
        )
        self.update_panel_pause_status()

    def run_scheduled_update(self, _):
        if self.timer is not None:
            self.timer.invalidate()
        self.update_status()

    def check_schedule_deadline(self, _):
        self.start_weather_refresh()
        self.update_weather_panel()
        if self.next_auto_update_at is not None and datetime.now() >= self.next_auto_update_at:
            self.run_scheduled_update(None)
        else:
            self.apply_weather_status_if_needed()

    def load_pause_until(self):
        if not PAUSE_FILE.exists():
            return None

        try:
            pause_until = datetime.fromisoformat(PAUSE_FILE.read_text().strip())
        except (OSError, ValueError):
            return None

        if pause_until <= datetime.now():
            PAUSE_FILE.unlink(missing_ok=True)
            return None

        return pause_until

    def auto_pause_status_text(self):
        if self.vacation_mode:
            return "Vacation mode: on | Schedule suspended"
        pause_until = self.load_pause_until()
        if pause_until is None:
            return "Auto updates: active"
        return f"Auto updates: paused until {format_pause_until(pause_until)}"

    def auto_updates_are_paused(self):
        return self.load_pause_until() is not None

    def pause_auto_updates_until(self, pause_until, notify=True):
        PAUSE_FILE.write_text(pause_until.isoformat(timespec="seconds"))
        message = f"Auto updates paused until {format_pause_until(pause_until)}"
        self.update_panel_pause_status()
        self.update_panel_status(message)
        self.schedule_next_auto_update()
        if notify:
            rumps.notification(
                title="Slack Status Auto Updates Paused",
                subtitle="",
                message=message,
                sound="Ping",
            )

    def pause_auto_updates_from_prompt(self, _=None):
        if self.vacation_mode:
            self.update_panel_status("Vacation mode stays on until you turn it off")
            return
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
        if self.vacation_mode:
            self.update_panel_status("Turn off Vacation mode to resume the schedule")
            return
        try:
            PAUSE_FILE.unlink()
        except FileNotFoundError:
            pass

        self.update_status(notify=False, ignore_pause=True)
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
        self.default_status_button.setTitle_("Default")
        self.default_status_button.setToolTip_(f"Apply scheduled status: {default_label}")
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
        self.start_weather_refresh(force=True)
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
        if self.vacation_mode:
            self.update_panel_status("Turn off Vacation mode to set a custom status")
            return
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

    def set_preset_status_by_index(self, index):
        if self.vacation_mode:
            self.update_panel_status("Turn off Vacation mode to choose a preset")
            return
        if index < 0 or index >= len(self.status_presets):
            self.update_panel_status("Preset unavailable")
            return

        preset = self.status_presets[index]
        response = rumps.Window(
            title=f"Keep {preset['label']} Until",
            message="Keep until: next change, 1h, 2h, 90m, tomorrow, or a date/time.",
            default_text="next change",
            cancel="Cancel",
        ).run()
        if not response.clicked:
            return
        value = response.text.strip()
        pause_until = (
            next_schedule_change_after(self.status_schedule)
            if value.lower() in {"next", "next change"}
            else parse_pause_until(value)
        )
        if pause_until is None or pause_until <= datetime.now():
            rumps.alert(title="Invalid Hold Time", message="Enter a future time or next change.")
            return
        if self.set_slack_status(
            preset["emoji"],
            preset.get("status_text", "") or preset["label"],
            notify=False,
        ):
            self.pause_auto_updates_until(pause_until, notify=False)

    @rumps.clicked("Pause Auto Updates")
    def pause_auto_updates_clicked(self, _):
        self.pause_auto_updates_from_prompt()

    @rumps.clicked("Resume Auto Updates")
    def resume_auto_updates_clicked(self, _):
        self.resume_auto_updates()

    def refresh_vacation_controls(self):
        self.menu["Vacation Mode"].state = self.vacation_mode
        if self.panel_vacation_toggle is not None:
            self.panel_vacation_toggle.setState_(int(self.vacation_mode))

    @rumps.clicked("Vacation Mode")
    def toggle_vacation_mode(self, _=None):
        enabled = not self.vacation_mode
        try:
            save_vacation_mode(enabled)
        except OSError as error:
            self.refresh_vacation_controls()
            self.update_panel_status(f"Could not save Vacation mode: {error}")
            return
        self.vacation_mode = enabled
        self.refresh_vacation_controls()
        if not enabled:
            PAUSE_FILE.unlink(missing_ok=True)
        self.update_status(notify=False, ignore_pause=True)

    def update_status(self, _=None, notify=True, ignore_pause=False, refresh_weather=True):
        self.refresh_default_status_button()
        if self.vacation_mode:
            success = self.set_slack_status(":palm_tree:", "", notify=notify)
            self.schedule_next_auto_update(retry=not success)
            return
        if not ignore_pause and self.auto_updates_are_paused():
            self.update_panel_status(self.auto_pause_status_text())
            self.schedule_next_auto_update()
            return

        entry = schedule_entry_for_time(self.status_schedule)
        text = entry.get("status_text", "") or entry["label"]
        emoji = entry["emoji"]
        if entry.get("weather_aware"):
            if refresh_weather:
                self.start_weather_refresh(force=True)
            emoji, text = self.weather_state.decision().status
        success = self.set_slack_status(emoji, text, notify=notify)
        self.weather_status_retry_until = None if success else datetime.now() + timedelta(seconds=60)
        self.schedule_next_auto_update(retry=not success)

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
            self.last_applied_status = (applied_emoji, status_text)
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
