"""Cooperative checkpoints for web-managed worker processes."""
import os
import time
from pathlib import Path


def wait_if_paused():
    marker = os.environ.get("INTENT_JOB_PAUSE_FILE")
    if marker:
        path = Path(marker)
        while path.is_file():
            time.sleep(0.2)
