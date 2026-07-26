"""Shared Anthropic client. Haiku 4.5 for all classification/layout calls (see decisions log)."""

from __future__ import annotations

import os

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

MODEL_HAIKU = "claude-haiku-4-5-20251001"

_client: Anthropic | None = None


def get_client() -> Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Get one from console.anthropic.com, then either "
                "`export ANTHROPIC_API_KEY=...` or put it in a .env file in the project root."
            )
        _client = Anthropic(api_key=api_key)
    return _client
