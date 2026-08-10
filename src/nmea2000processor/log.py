"""Tiny stdlib logging helper: prefixes every progress line with the current date/time, so a run
against a slow network (downloading from the W2K-2) or a large dataset (parsing hundreds of .ebl
files) makes it visible how much time each step actually costs.
"""

from __future__ import annotations

import sys
from datetime import datetime
from typing import TextIO


def log(message: str, *, file: TextIO = sys.stdout) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{timestamp} {message}", file=file)
