import os
from dotenv import load_dotenv
import rumps
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from datetime import datetime
import ssl
import certifi

# Load environment variables from .env file
load_dotenv()

class SlackStatusApp(rumps.App):
    def __init__(self):
        super(SlackStatusApp, self).__init__("Slack Status")
        self.menu = ["Update Now", "Set Custom Status", "Quit"]

        ssl_context = ssl.create_default_context(cafile=certifi.where())
        slack_token = os.getenv("SLACK_API_TOKEN")
        if not slack_token:
            rumps.alert(title="Slack Status — Missing Token",
                        message="Environment variable SLACK_API_TOKEN is not set. Please add it to your .env or environment.")
        self.client = WebClient(token=slack_token, ssl=ssl_context)

        self.update_status()
        rumps.timer(3600)(self.update_status)

    @rumps.clicked("Update Now")
    def update_now(self, _):
        self.update_status()

    @rumps.clicked("Set Custom Status")
    def set_custom_status(self, _):
        response = rumps.Window(
            title="Custom Slack Status",
            message="Enter status emoji and optional text separated by a comma\n(e.g., :tornado:, Working remotely)",
            default_text=":tornado:, Working remotely"
        ).run()

        if response.clicked:
            user_input = response.text.strip()
            parts = [part.strip() for part in user_input.split(",", 1)]
            if len(parts) == 2:
                emoji, status_text = parts
                self.set_slack_status(emoji, status_text)
            elif len(parts) == 1:
                emoji = parts[0]
                self.set_slack_status(emoji)

    def update_status(self, _=None):
        current_hour = datetime.now().hour
        current_day = datetime.now().weekday()  # Monday is 0, Sunday is 6

        # Sunday and Saturday
        if current_day == 6 or current_day == 5:
            if 7 <= current_hour < 20:
                self.set_slack_status(":tornado:")
            elif 20 <= current_hour or current_hour < 4:
                self.set_slack_status(":sleeping:")
            else:
                self.set_slack_status(":briefcase:")
            if 14 <= current_hour < 20:
                self.set_slack_status(":tornado:")
        else:  # Monday to Friday
            if 4 <= current_hour < 14:
                self.set_slack_status(":briefcase:")
            elif 14 <= current_hour < 20:
                self.set_slack_status(":tornado:")
            elif 20 <= current_hour or current_hour < 4:
                self.set_slack_status(":sleeping:")

    def set_slack_status(self, emoji, status_text=""):
        try:
            self.client.users_profile_set(
                profile={
                    "status_text": status_text,
                    "status_emoji": emoji,
                    "status_expiration": 0
                }
            )
            if emoji == ":tornado:":
                sound_name = "Funk"
            elif emoji == ":briefcase:":
                sound_name = "Ping"
            elif emoji == ":sleeping:":
                sound_name = "Basso"
            else:
                sound_name = "Submarine"

            rumps.notification(
                title="Slack Status Updated",
                subtitle="",
                message=f"Status set to {emoji} {status_text}".strip(),
                sound=sound_name
            )
        except SlackApiError as e:
            rumps.alert(title="Error", message=f"Failed to update status: {e.response['error']}")

if __name__ == "__main__":
    app = SlackStatusApp()
    app.run()