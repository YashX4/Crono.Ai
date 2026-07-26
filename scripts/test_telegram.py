"""Sends one real test notification to your phone via Telegram, with the 3-option menu
wired to a fake block_id (safe to tap — the server just won't find a matching block and
no-ops).

Routes the send through the running server's /debug/send-test-checkin endpoint rather
than calling telegram_notifier directly — the button-tap token only exists in whichever
process's memory registered it, so a standalone script sending directly can never have
its buttons actually work even with the server's poll loop running. The server MUST be
running for this to work.

Run with: .venv/bin/python scripts/test_telegram.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from dotenv import load_dotenv

load_dotenv()


def main():
    token = os.environ.get("WEBHOOK_TOKEN")
    if not token:
        print("WEBHOOK_TOKEN not set in .env")
        return

    try:
        response = httpx.post(
            "http://localhost:8787/debug/send-test-checkin",
            json={"token": token},
            timeout=15,
        )
    except httpx.ConnectError:
        print("Couldn't reach the server on localhost:8787 — is it running?")
        return

    print(f"HTTP {response.status_code}")
    print(response.json())
    if response.status_code == 200:
        print("Sent. Check your phone and tap a button — the server log should show it land.")


if __name__ == "__main__":
    main()
