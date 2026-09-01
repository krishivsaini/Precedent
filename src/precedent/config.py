"""Environment configuration, loaded from `.env` (gitignored) plus the process environment.

Reading is lazy (`razorpay_config()` is only evaluated when actually called) so that
importing this module — or running any test that doesn't touch Razorpay — never requires
credentials to be present.
"""

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class RazorpayConfig:
    key_id: str
    key_secret: str
    webhook_secret: str


def gemini_api_key() -> str:
    """The Gemini key, or an empty string if unset.

    Returns empty rather than raising, deliberately: a missing key has to surface at the
    call site as `LLMUnavailable` so a batch escalates the remaining cases and keeps the
    results it has, instead of dying part-way through. See `adapters/llm/gemini.py`.

    Env access lives here rather than in the adapter so `load_dotenv()` above is the single
    place `.env` is read — an adapter reading `os.environ` directly would work under pytest
    and silently find nothing from a bare CLI entry point.
    """
    return os.environ.get("GEMINI_API_KEY", "")


def groq_api_key() -> str:
    """The Groq key, or an empty string if unset. Same contract as `gemini_api_key`."""
    return os.environ.get("GROQ_API_KEY", "")


def nvidia_api_key() -> str:
    """The NVIDIA NIM key (build.nvidia.com), or an empty string if unset."""
    return os.environ.get("NVIDIA_API_KEY", "")


@lru_cache
def razorpay_config() -> RazorpayConfig:
    values = {
        "RAZORPAY_KEY_ID": os.environ.get("RAZORPAY_KEY_ID"),
        "RAZORPAY_KEY_SECRET": os.environ.get("RAZORPAY_KEY_SECRET"),
        "RAZORPAY_WEBHOOK_SECRET": os.environ.get("RAZORPAY_WEBHOOK_SECRET"),
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ConfigError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Copy .env.example to .env and fill them in with your Razorpay TEST-MODE credentials."
        )
    return RazorpayConfig(
        key_id=values["RAZORPAY_KEY_ID"],
        key_secret=values["RAZORPAY_KEY_SECRET"],
        webhook_secret=values["RAZORPAY_WEBHOOK_SECRET"],
    )
