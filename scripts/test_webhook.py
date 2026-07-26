"""Sends a test check-in answer to the running server's webhook. Reads WEBHOOK_TOKEN via
python-dotenv (robust regardless of exact .env formatting) and makes the HTTP request
directly — no shell variable interpolation of secrets, no `source .env`.

Usage: .venv/bin/python scripts/test_webhook.py <block_id> <answer>
  answer is one of: completed | running_behind | unexpected_plan

Run scripts/list_agent_blocks.py first to get a real block_id.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from dotenv import load_dotenv

load_dotenv()


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <block_id> <completed|running_behind|unexpected_plan>")
        return

    block_id, answer = sys.argv[1], sys.argv[2]
    token = os.environ.get("WEBHOOK_TOKEN")
    if not token:
        print("WEBHOOK_TOKEN not set in .env")
        return

    response = httpx.post(
        "http://localhost:8787/webhook/checkin",
        json={"token": token, "block_id": block_id, "answer": answer},
        timeout=30,
    )
    print(f"HTTP {response.status_code}")
    print(response.json())


if __name__ == "__main__":
    main()
