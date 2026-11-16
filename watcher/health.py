"""Docker healthcheck: healthy while the heartbeat file is fresh.

The scheduler writes it every minute, but only while the Telegram connection is up."""

import os
import sys
import time
from pathlib import Path

MAX_AGE = 300


def main() -> int:
    path = Path(os.environ.get("DATA_DIR", "/data")) / "heartbeat"
    try:
        age = time.time() - int(path.read_text().strip())
    except (OSError, ValueError):
        print("no heartbeat yet")
        return 1
    if age > MAX_AGE:
        print(f"heartbeat is {int(age)}s old")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
