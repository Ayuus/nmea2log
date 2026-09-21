"""Checks the names and counts of the .ebl files per folder, on the machine that decodes them.

The W2K-2 stores its log files as ``EBL000012/000012_007.ebl``: folders numbered from 0, each holding
100 files (``_000`` to ``_099``) except the newest, whose number is the folder's own. The download
already compares its own listing per folder with what the W2K-2 offers; this looks at what is actually
on disk when the logbook is built, so a file that is missing, misnamed or in the wrong folder shows up
as a loud ``[anomaly]`` instead of as a quietly incomplete season. Folders that do not follow the
``EBLnnnnnn`` naming (a hand-made directory of copies, the tests) are left alone: nothing is assumed
about them.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List, Sequence

from .log import log

FILES_PER_FOLDER = 100

_FOLDER_NAME = re.compile(r"^EBL(\d{6})$")
_FILE_NAME = re.compile(r"^(\d{6})_(\d{3})\.ebl$")
_MAX_LISTED = 6


def _listed(names: Sequence[str]) -> str:
    """The first few names, with how many more there are."""
    shown = ", ".join(names[:_MAX_LISTED])
    return shown + (f" and {len(names) - _MAX_LISTED} more" if len(names) > _MAX_LISTED else "")


def _file_name(folder_number: int, sequence: int) -> str:
    return f"{folder_number:06d}_{sequence:03d}.ebl"


def check_logfile_layout(logfiles: Sequence[Path]) -> List[str]:
    """The problems found, as one line each (empty when the files are complete and correctly named); only
    files inside an ``EBLnnnnnn`` folder are looked at."""
    sequences_by_folder: Dict[int, List[int]] = {}
    problems: List[str] = []
    for path in logfiles:
        folder = _FOLDER_NAME.match(path.parent.name)
        if folder is None:
            continue
        folder_number = int(folder.group(1))
        name = _FILE_NAME.match(path.name)
        if name is None:
            problems.append(f"{path.parent.name}/{path.name}: the name is not of the form NNNNNN_MMM.ebl")
        elif int(name.group(1)) != folder_number:
            problems.append(f"{path.parent.name}/{path.name}: the number in the name does not match its folder")
        else:
            sequences_by_folder.setdefault(folder_number, []).append(int(name.group(2)))
    if not sequences_by_folder:
        return problems

    first_folder, last_folder = min(sequences_by_folder), max(sequences_by_folder)
    missing_folders = [
        f"EBL{n:06d}" for n in range(first_folder, last_folder + 1) if n not in sequences_by_folder
    ]
    if missing_folders:
        problems.append(f"folder(s) missing: {_listed(missing_folders)}")
    for folder_number in sorted(sequences_by_folder):
        present = set(sequences_by_folder[folder_number])
        # Every folder but the newest is full; the oldest may start later (older files removed on purpose).
        first = min(present) if folder_number == first_folder else 0
        last = max(present) if folder_number == last_folder else FILES_PER_FOLDER - 1
        missing = [_file_name(folder_number, s) for s in range(first, last + 1) if s not in present]
        if missing:
            problems.append(f"EBL{folder_number:06d}: {len(missing)} file(s) missing: {_listed(missing)}")
    return problems


def log_logfile_layout(logfiles: Sequence[Path]) -> None:
    """One ``[info]`` line with the totals when everything is in order, else an ``[anomaly]`` per problem."""
    folders = {path.parent.name for path in logfiles if _FOLDER_NAME.match(path.parent.name)}
    if not folders:
        return
    problems = check_logfile_layout(logfiles)
    for problem in problems:
        log(
            f"[anomaly] Log files: {problem} -- the season may be incomplete; download again, or check the "
            "folder on the W2K-2.",
            file=sys.stderr,
        )
    if not problems:
        ordered = sorted(folders)
        log(
            f"[info] Log files: {len(logfiles)} .ebl file(s) in {len(folders)} folder(s) "
            f"({ordered[0]} to {ordered[-1]}), none missing.",
            file=sys.stderr,
        )
