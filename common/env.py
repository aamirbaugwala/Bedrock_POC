"""Load .env and normalise AWS env vars so boto3 picks them up cleanly."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    # Values already in the environment win, so `AWS_REGION=us-east-1 make teardown` targets that region.
    load_dotenv(ROOT / ".env", override=False)
    # Blank values (e.g. an unused AWS_SESSION_TOKEN=) confuse botocore; drop them.
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "MODEL_ID"):
        if key in os.environ and not os.environ[key].strip():
            del os.environ[key]
    os.environ.setdefault("AWS_REGION", "us-east-1")
    os.environ["AWS_DEFAULT_REGION"] = os.environ["AWS_REGION"]


def state_path() -> Path:
    """Per-region record of created resources, so several regions can be deployed side by side."""
    return ROOT / f".demo-state.{os.environ['AWS_REGION']}.json"
