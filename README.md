# Slack Status Menu Bar App

This macOS menu bar application allows you to update your Slack status quickly with preset messages like "In a meeting", "Be right back", and "Working remotely". Built with Python and rumps (Ridiculously Uncomplicated macOS Python Statusbar apps), it provides a lightweight interface for interacting with Slack from your desktop.

## 📦 Features

- 🖥️ macOS menu bar integration
- ⚡ One-click Slack status updates
- 🔐 Environment-based Slack token security
- 🧪 Easy to extend with your own status presets

## 🚀 Installation

1. **Clone the repository:**

   ```bash
   git clone https://github.com/yourusername/slack-status-menubar.git
   cd slack-status-menubar
   
2.	Install dependencies:
It is recommended to use a virtual environment:
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
3. Configure your environment:
Create a .env file in the project root and add your Slack token:
SLACK_TOKEN=xoxp-your-slack-token-here 

## 🛠️  Usage

Once the app is running, you’ll see a Slack icon in your menu bar. Click it to reveal preset status options. Selecting a status will update your Slack status instantly.

## 📋️  Requirements
	•	Python 3.9+
	•	macOS
	•	Slack user OAuth token (xoxp-)

## 🧪Development

Feel free to fork and contribute! Add your own status messages, hook in Apple Shortcuts, or expand to support multiple Slack workspaces.

## 📜License

This project is licensed under the GNU GPL v3.0.