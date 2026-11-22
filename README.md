# Slack Status Menu Bar App

A small macOS menu bar app that keeps your Slack status aligned with your day. It sets a default status on launch, refreshes on a timer, lets you choose quick presets from a floating control panel, and supports custom Slack emoji/status text.

## Features

- macOS menu bar app built with Python, rumps, and PyObjC
- Floating control panel with Work, Storms, Sleep, Custom, and current default actions
- Configurable weekday/weekend status schedule
- Updates at schedule boundaries, with a check for missed changes after waking
- Next status change or manual hold end time shown in the control panel
- Weather-aware availability for Tazewell County, Illinois
- Configurable quick-action status presets
- Slack profile readback on launch so the panel shows the current Slack status
- Single-instance behavior: launching the script again reopens the existing control panel
- Environment-based Slack token loading from `.env` or your shell

## Installation

1. Create and activate a virtual environment:

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Create a `.env` file in the project root:

   ```bash
   SLACK_API_TOKEN=xoxp-your-slack-user-token
   ```

   `SLACK_TOKEN` is also supported for older local setups.

## Usage

Run the app:

```bash
./run-main.sh
```

The control panel opens on launch. Use **Default** to apply the schedule-based status, choose a preset, or enter a custom status in the format:

```text
🌪, Working remotely
```

When you set a custom status, automatic schedule updates pause until the next schedule change so the custom status is not immediately overwritten.

Launch `./run-main.sh` again while the app is already running to bring the existing control panel back to the front.

## Status Schedule

The default schedule lives in `status_schedule.json`:

- Weekdays: Work from 7:00-15:00, Storms from 15:00-21:00, Sleep from 21:00-7:00
- Weekends: Coffee from 6:00-8:00, Storms from 8:00-21:00, Sleep from 21:00-6:00

Edit `status_schedule.json` to change labels, Slack emoji aliases, or time windows, then restart the app. Hours use 24-hour local time. A window can cross midnight by setting `start` later than `end`, such as `20` to `4`.

If the config file is missing or invalid, the app falls back to the built-in default schedule.

Scheduled statuses use optional `status_text` (or their label when omitted). Work posts `At work` and Sleep posts `Sleeping`. Weather-aware availability is described below.

## Weather-Aware Availability

The existing Storms schedule entries have `weather_aware: true`. During those windows (weekdays 15:00-21:00 and weekends 08:00-21:00), the app chooses:

- Tornado emoji with `Available for spotting` when any part of Tazewell County, Illinois intersects a current Day 1 SPC Marginal-or-higher severe-weather outlook, a Day 1 WPC Marginal-or-higher excessive-rainfall outlook, or has a qualifying active NWS alert.
- Green circle with `Available` otherwise. General thunderstorm outlooks alone do not qualify.

Qualifying NWS alerts are tornado and severe-thunderstorm watches/warnings, flood watches/warnings, and flash-flood watches/warnings. Both the county code `ILC179` and forecast zone `ILZ037` are checked, covering rainfall and river-flood alerts. Outlook intersection uses the NWS county boundary, not just one town or the county center.

Weather requests run in the background at startup, every 30 minutes, and when scheduled availability is applied. Changes update Slack only during an availability slot and never during a manual hold. Work, Sleep, Coffee, and manually chosen presets keep their existing wording and emoji. The manual Storms button remains a way to select spotting mode yourself.

The manual **Available** preset posts `Available` with a green circle in Slack and the menu bar, and uses the same hold-duration prompt as other presets.

The panel shows the weather reason and check time; hover over that line for complete reasons and source errors. If a source fails, its cached result can be used for up to two hours, but never beyond the outlook/alert expiration. Any usable positive source still enables spotting. If all positive evidence is expired or unavailable, the app uses neutral `Available` and shows `Weather unavailable` rather than claiming the forecast is clear. Cached weather results survive app restarts in the app's temporary runtime directory.

Official data sources:

- [SPC Day 1 categorical outlook](https://mapservices.weather.noaa.gov/vector/rest/services/outlooks/SPC_wx_outlks/MapServer/1)
- [WPC Day 1 excessive-rainfall outlook](https://mapservices.weather.noaa.gov/vector/rest/services/hazards/wpc_precip_hazards/MapServer/0)
- [NWS API forecasts, county boundaries, and alerts](https://www.weather.gov/documentation/services-web-api)

## Status Presets

Quick-action buttons are loaded from `status_presets.json`. Each preset has a button label, Slack emoji, and optional status text:

```json
{
  "label": "Phone",
  "emoji": ":phone:",
  "status_text": "On a call"
}
```

Edit `status_presets.json`, then restart the app. If the file is missing or invalid, the app falls back to Work, Storms, Sleep, and Coffee.

Presets post their status text (or button label when text is empty). Custom statuses entered manually keep exactly the text you provide.

Choosing a preset asks how long to keep it: `next change` (the default), `1h`, `2h`, `90m`, `tomorrow`, or a future date/time. Cancel leaves the current status unchanged. A successful preset pauses automation until that time; the scheduled status then returns automatically.

## Pausing Auto Updates

### Vacation Mode

Turn on **Vacation mode** in the control window or **Vacation Mode** in the menu bar menu to post only the palm tree emoji, with empty Slack status text. It also shows a palm tree in the menu bar.

Vacation mode overrides all scheduled and weather-based statuses, including after quitting, restarting, or rebooting. It has no expiration. While enabled, presets, custom status controls, and Resume Auto cannot replace it. Turn the Vacation mode switch off to immediately return to the current scheduled status and clear any older timed pause.

The setting is saved in `~/Library/Application Support/Slack Status Updater/vacation.json`. Location Services are not used.

Use **Pause Auto...** in the control panel to stop scheduled status updates until a future time. Manual buttons still work while auto updates are paused.

Accepted pause inputs:

- `2h`
- `90m`
- `tomorrow`
- `18:30`
- `2026-06-21 18:30`

Use **Resume Auto** to turn scheduled updates back on and apply the current scheduled status immediately.

## Menulet Icons

The app uses bundled PNG icons for the main statuses. For other Slack emoji aliases, it will try to show the matching Unicode emoji glyph in the menu bar.

To add a custom menu bar PNG for another status, place a 64 x 64 transparent PNG in `assets` using this naming pattern:

```text
assets/status_<emoji_alias>.png
```

Examples:

- `:phone:` -> `assets/status_phone.png`
- `:calendar:` -> `assets/status_calendar.png`
- `:spiral_calendar_pad:` -> `assets/status_spiral_calendar_pad.png`

If no matching PNG or Unicode glyph exists, the app falls back to the generic Slack status icon.

## Requirements

- macOS
- Python 3.9+
- Slack user OAuth token with permission to update your user profile status

## Development

The active app is `main.py`. `legacy-menu-bar.py` is kept only as an older reference implementation.

## Acknowledgment

Development of this project included AI-assisted coding support from ChatGPT/Codex. Final decisions, configuration, and deployment remain maintained by the project owner.

## License

This project is licensed under the GNU GPL v3.0.
