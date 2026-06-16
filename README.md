# Slack Status Menu Bar App

A small macOS menu bar app that keeps your Slack status aligned with your day. It sets a default status on launch, refreshes on a timer, lets you choose quick presets from a floating control panel, and supports custom Slack emoji/status text.

## Features

- macOS menu bar app built with Python, rumps, and PyObjC
- Floating control panel with Work, Storms, Sleep, Custom, and current default actions
- Configurable weekday/weekend status schedule
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

Launch `./run-main.sh` again while the app is already running to bring the existing control panel back to the front.

## Status Schedule

The default schedule lives in `status_schedule.json`:

- Weekdays: Work from 4:00-14:00, Storms from 14:00-20:00, Sleep from 20:00-4:00
- Weekends: Coffee from 4:00-7:00, Storms from 7:00-20:00, Sleep from 20:00-4:00

Edit `status_schedule.json` to change labels, Slack emoji aliases, or time windows, then restart the app. Hours use 24-hour local time. A window can cross midnight by setting `start` later than `end`, such as `20` to `4`.

If the config file is missing or invalid, the app falls back to the built-in default schedule.

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
