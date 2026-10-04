"""What both apps tell the owner when a download / assemble / publish run ends, and whether a publish
counts as failed.

The Android and iOS apps each decided this on their own and had drifted apart: iOS showed "no W2K-2 found"
and "no .ebl files" as errors where Android reports them calmly, lacked the closing "Voltooid" line, worded
the result differently, and counted "nothing set up to publish to" as a failed publish (which kept the log
over the new logbook). The decision is made here once; an app only turns the answer into its own widgets.

The result dict is what android_entry returns (``ok``, ``cancelled``, ``error``, ``error_kind``,
``trip_count``, ``downloaded_count``). The texts are keys into ``app_texts.TEXTS``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# What the app does with the screen once the lines are logged.
SHOW_LOGBOOK = "logbook"  # the new logbook (with the log as a strip next to it)
SHOW_LOG = "log"  # the log stays in front
SHOW_EXISTING_LOGBOOK = "existing_logbook"  # the logbook that was already there, if any
SHOW_ERROR = "error"  # the log, and the app may point out the error

W2K2_NOT_FOUND = "w2k2_not_found"
NO_EBL_FILES = "no_ebl_files"

# A result from before error_kind existed (the Android app's Kotlin side only carries the text).
_LEGACY_ERROR_KINDS = (
    ("No W2K-2 found", W2K2_NOT_FOUND),
    ("No .ebl files given.", NO_EBL_FILES),
)


@dataclass
class Line:
    """One log line: ``level`` is the tag ("info", "error"), ``key`` a key of app_texts.TEXTS, or ``text``
    a line that is shown as it is (an error message from Python)."""

    level: str
    key: Optional[str] = None
    params: Dict[str, object] = field(default_factory=dict)
    text: Optional[str] = None


@dataclass
class RunOutcome:
    lines: List[Line]
    show: str


def error_kind(result: dict) -> Optional[str]:
    kind = result.get("error_kind")
    if kind:
        return kind
    error = result.get("error") or ""
    for prefix, legacy_kind in _LEGACY_ERROR_KINDS:
        if error.startswith(prefix):
            return legacy_kind
    return None


def describe_result(result: dict, initiator: str, publish_failed: bool = False) -> RunOutcome:
    """The log lines for the end of a run and what to show. ``initiator`` is "download" or anything else
    (assemble, publish) -- it only chooses between the "download stopped" and "assembling stopped" texts."""
    if result.get("ok"):
        trip_count = result.get("trip_count")
        downloaded = result.get("downloaded_count")
        if trip_count is None or trip_count < 0:
            line = Line("info", "status_ready_updated")
        elif downloaded is not None and downloaded >= 0:
            line = Line("info", "status_ready_with_download", {"trips": trip_count, "files": downloaded})
        else:
            line = Line("info", "status_ready_no_download", {"trips": trip_count})
        return RunOutcome([line], SHOW_LOG if publish_failed else SHOW_LOGBOOK)

    if result.get("cancelled"):
        key = "status_sync_stopped" if initiator == "download" else "status_build_stopped"
        return RunOutcome([Line("info", key)], SHOW_LOG)

    kind = error_kind(result)
    if kind == W2K2_NOT_FOUND:
        # Not at the boat: expected and common, so a calm line and the logbook that is already there.
        return RunOutcome([Line("info", text=result.get("error"))], SHOW_EXISTING_LOGBOOK)
    if kind == NO_EBL_FILES:
        return RunOutcome([Line("info", "log_no_ebl_files_to_build")], SHOW_LOG)

    detail = result.get("error")
    if detail:
        return RunOutcome([Line("error", "error_generic_prefix", {"error": detail})], SHOW_ERROR)
    return RunOutcome([Line("error", "error_generic_prefix", {"error": None})], SHOW_ERROR)


def publish_failed(published: bool, rest_complete: bool, sftp_complete: bool) -> bool:
    """Whether the publish after a build failed: only an upload that was attempted (a destination is set
    up) and did not succeed. Nothing set up to publish to is not a failure -- the logbook is shown as usual."""
    return not published and (rest_complete or sftp_complete)


def describe_result_json(result_json: str, initiator: str, publish_failed_flag: bool) -> str:
    """describe_result() for the Android app, which passes the result and gets the outcome back as JSON
    (Chaquopy hands Python objects over poorly, the same reason bootmode has a JSON entry point).
    ``{"lines": [{"level", "key", "params", "text"}], "show"}``."""
    outcome = describe_result(json.loads(result_json), initiator, publish_failed_flag)
    return json.dumps(
        {
            "lines": [
                {"level": line.level, "key": line.key, "params": line.params, "text": line.text}
                for line in outcome.lines
            ],
            "show": outcome.show,
        }
    )
