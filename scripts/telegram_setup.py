"""One-time setup: fetches your chat_id after you've messaged your bot at least once.

1. Create a bot via @BotFather in Telegram (/newbot), get a token.
2. Put TELEGRAM_BOT_TOKEN=<token> in .env.
3. Open your new bot in Telegram and send it any message (e.g. "/start").
4. Run: .venv/bin/python scripts/telegram_setup.py
5. It'll print your chat_id — add TELEGRAM_CHAT_ID=<id> to .env.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from dotenv import load_dotenv

load_dotenv()


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Set TELEGRAM_BOT_TOKEN in .env first.")
        return

    response = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15)
    response.raise_for_status()
    updates = response.json().get("result", [])

    chats = {}
    for update in updates:
        message = update.get("message")
        if message:
            chat = message["chat"]
            chats[chat["id"]] = chat.get("username") or chat.get("first_name", "?")

    if not chats:
        print("No messages found yet. Open your bot in Telegram and send it any message, then run this again.")
        return

    print("Found chat(s):")
    for chat_id, name in chats.items():
        print(f"  chat_id={chat_id}  ({name})")
    print("\nAdd TELEGRAM_CHAT_ID=<the chat_id above> to .env")


if __name__ == "__main__":
    main()
