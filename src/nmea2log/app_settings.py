"""The settings of the Android and iOS apps: names, default values, validation, and when a group of
settings counts as filled in.

The defaults used to be written out in the Android SettingsStore, the iOS settings store, both settings
screens and bootmode.BootModeConfig, on five or more places each, and the apps had begun to differ (the iOS
screen limited the boat-mode minutes to at least 1, the Android one did not). They are defined here once: iOS imports
this module, and the Android app gets the values as a generated Kotlin file (export_android_constants.py), as
its settings are read before Python has started.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# Same as build_arg_parser()'s own --min-stop-minutes (cli.py).
DEFAULT_MIN_STOP_MINUTES = 10.0

# What the "round every ..." choice of the boat mode offers, in minutes.
BOOT_INTERVAL_CHOICES: Tuple[int, ...] = (30, 60, 120, 180)

# Boat-mode durations are whole minutes, at least this.
MINIMUM_MINUTES = 1

DEFAULTS: Dict[str, Any] = {
    "w2k2_user": "",
    "w2k2_password": "",
    "boat_name": "",
    "mmsi": "",
    "call_sign": "",
    # Off by default: the owner chooses whether opening the app tries to reach the W2K-2 at once.
    "auto_sync_on_launch": False,
    "auto_publish_after_build": True,
    "min_stop_minutes": DEFAULT_MIN_STOP_MINUTES,
    # Never defaulted: a new install must not show a real server host or path.
    "rest_upload_url": "",
    "rest_upload_user": "",
    "rest_upload_password": "",
    # Whether publishing is switched on ("WordPress" picked in Settings), kept apart from the WordPress details:
    # picking "don't publish" must not wipe them. On by default: a install from before this setting, with its
    # details filled in, goes on publishing.
    "publish_enabled": True,
    # The boat mode; the same names and values as bootmode.BootModeConfig.
    "boot_round_interval_minutes": 60,
    "boot_publish_every_round": False,
    "boot_final_on_harbour": True,
    "boot_harbour_stationary_minutes": 30,
    "boot_harbour_engine_off_minutes": 10,
    "boot_final_on_left_boat": True,
    "boot_left_boat_minutes": 20,
    "boot_stop_after_final": False,
    "boot_auto_start": False,
    # "light" / "dark" / "system".
    "theme_mode": "system",
}


def parse_int(text: object, default: int, minimum: Optional[int] = None) -> int:
    """``text`` as a whole number (at least ``minimum``), or ``default`` when it is not one."""
    try:
        value = int(text)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default
    return value if minimum is None else max(minimum, value)


def parse_float(text: object, default: float) -> float:
    """``text`` as a number, or ``default`` when it is not one."""
    try:
        return float(text)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _filled(*values: str) -> bool:
    return all(bool(value.strip()) for value in values)


def is_w2k2_complete(user: str, password: str) -> bool:
    return _filled(user, password)


def is_rest_complete(url: str, user: str, password: str) -> bool:
    return _filled(url, user, password)


def is_publish_configured(enabled: bool, url: str, user: str, password: str) -> bool:
    """Whether a logbook is published: switched on in Settings, and the WordPress details are all there."""
    return enabled and is_rest_complete(url, user, password)
