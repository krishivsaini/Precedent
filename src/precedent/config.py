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
