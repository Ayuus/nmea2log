"""Importing .ebl files from an SD card or USB drive (as opposed to downloading them straight
from the W2K-2) -- shared by both apps' own import buttons (Android's MainActivity.kt, iOS's
app.py) via Chaquopy/plain Python respectively, so this collision/placement logic exists exactly
once instead of being ported twice.

Each platform is responsible for its own SAF/UIDocumentPicker access and stages the picked files
into a local scratch directory it fully controls first (neither a content:// Uri nor an
NSFileCoordinator-read tree is something plain Python can open directly) -- but it preserves each
source file's own immediate parent folder name when staging it, rather than flattening everything
into one directory: this module tells an original W2K-2 "EBLnnnnnn" folder apart from an
arbitrary one by that name (plus the file's own "nnnnnn_nnn.ebl" name, the same two patterns
logfile_layout.py itself checks the finished archive against), and needs it intact to place things
correctly.

Placement: ``<Actisense>/<year>/<EBLnnnnnn or nothing>/<original filename>`` -- the year comes
from the file's own first PGN 126992 (System Time) reading (see first_ebl_timestamp() below), not
a download/import date, so re-importing the same season's files later still lands them next to
each other. A file with no System Time message of its own has no year to go by; those land under
a literal "onbekend" folder instead of guessing.

Why a whole EBLnnnnnn folder is one placement decision, not one per file: sample_cache.py's own
cache key for a file in such a folder is (folder name, file name) -- not its full path -- and
logfile_layout.py's own season-completeness check groups files by the number in the folder name
alone, across the *entire* archive. Both assume "EBL000012" only ever means one thing archive-
wide. That assumption breaks the moment a W2K-2's SD card gets reformatted and its folder counter
starts over: a second, unrelated "EBL000012" would otherwise silently alias the first one in both
of those (cache corruption, corrupted completeness checks), regardless of which year folder either
one sits under. So an entire source EBLnnnnnn folder is imported as one unit: if none of its files
collide with a same-named, differently-content file already on disk, it's copied in under its own
name (growing an existing, still-open folder, or adding a new one -- the normal case); the moment
even one of its files does collide, the *whole* folder is renamed once, consistently
("EBL000012-<8 hex chars>"), which is enough on its own to stop matching either module's
EBLnnnnnn pattern and be treated as its own, unrelated thing from then on -- not a workaround
layered on top, the actual fix. A file that is already present with identical content is skipped
either way (re-importing the same card twice, or the same file surviving a reformat unchanged,
shouldn't double-count a trip).

Files that don't sit in a recognizable EBLnnnnnn folder (an SD card that doesn't mirror
Actisense's own layout at all -- see this repo's own README on why that's fine) have no such
archive-wide identity to protect, so each is placed and deduplicated on its own.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# Called once per file, right after (not before -- see import_staged_ebl_files()'s own doc
# comment) its outcome is decided, the same "real work, then report it" shape
# SyncController.report()/ProgressCallback.report() already use for a download: this is what lets
# a platform-side log/progress bar show one file at a time actually happening, instead of a
# whole batch's worth of already-decided outcomes replayed in a tight loop once this function has
# already returned (found in practice, a real regression -- a replay loop like that has nothing
# left to pace it, so however fast the platform side can iterate a finished list is how fast it
# went, no matter how that loop is throttled from the outside).
ImportProgressCallback = Callable[[int, int, str, str], None]

from .ebl_reader import iter_frames as iter_frames_ebl
from .pipeline import _WANTED_PGNS

# Same two patterns logfile_layout.py checks the finished archive against (kept in sync by hand,
# not imported -- that module's are private, and these are small and stable). Public here (unlike
# logfile_layout.py's own): the iOS app stages files for import in pure Python, in this same
# package, and imports these directly rather than keeping its own third copy in sync by hand too
# (Android's Kotlin staging code has no such option and keeps its own, see MainActivity.kt).
EBL_FOLDER_NAME = re.compile(r"^EBL\d{6}$")
EBL_FILE_NAME = re.compile(r"^\d{6}_\d{3}\.ebl$")

_UNKNOWN_YEAR = "onbekend"


def first_ebl_timestamp(path: Path) -> Optional[datetime]:
    """The file's own first resolvable time (its first PGN 126992 System Time reading -- the same
    time reference every other PGN in the file is already anchored to, see ebl_reader.py's own
    module doc comment), or None when the file has no System Time message of its own at all (then
    iter_frames() has nothing to yield -- every other PGN is dropped until a time reference
    exists). A fresh, file-local time_state each call, unlike the real decode pipeline's own use
    of iter_frames (see pipeline.py's _iter_frames_for_path()): this only ever asks one file, on
    its own, where it belongs, and carrying state across files here would make that answer depend
    on which other files happened to be peeked at first, and in what order -- exactly the kind of
    surprise this function exists to avoid. Stops at the first yielded frame (iter_frames is a
    generator; next() with a default here reads no further once it has one), not a full decode.
    """
    frame = next(iter_frames_ebl(path, wanted_pgns=_WANTED_PGNS), None)
    return frame.time if frame is not None else None


def _same_content(a: Path, b: Path) -> bool:
    return a.stat().st_size == b.stat().st_size and a.read_bytes() == b.read_bytes()


def _unique_path(dest: Path) -> Path:
    """dest, or the first "-1"/"-2"/... variant of it that doesn't already exist -- belt-and-
    suspenders should a same-named, different-content file ever reach here despite the
    folder-level rename above already having ruled that out for an EBLnnnnnn folder's own files
    (see the module doc comment); the only realistic way to hit this at all is a loose,
    non-EBLnnnnnn file whose name happens to collide with something already imported."""
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    n = 1
    while True:
        candidate = dest.with_name(f"{stem}-{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def _find_existing_ebl_folder(dest_root: Path, folder_name: str) -> Optional[Path]:
    """Any directory named exactly ``folder_name`` already under ``dest_root``, regardless of
    which year it sits under -- sample_cache.py's and logfile_layout.py's own EBLnnnnnn matching
    both look at the folder name alone, archive-wide, not the year above it, so the collision
    check below has to look the same way (a per-year-only check would miss a folder that was
    filed under a *different* year candidate than this batch's own, e.g. its own timestamps
    happened to average out differently across two imports of the same still-growing folder).

    Checks ``dest_root`` itself first, not only its year subfolders (found in practice, a real
    bug: every normal W2K-2 download -- from before this import feature existed, and still today,
    see EblStorage.downloadDir()/self.ebl_dir() -- writes ``EBLnnnnnn`` straight under dest_root,
    with no year layer above it; only an import's own destination ever gets one. Without this
    check, importing content whose W2K-2 already downloaded some of it normally couldn't find that
    existing folder at all -- read as "nothing here yet" -- and quietly duplicated it under a year
    folder instead of recognizing and deduplicating against it)."""
    if not dest_root.is_dir():
        return None
    direct = dest_root / folder_name
    if direct.is_dir():
        return direct
    for year_dir in dest_root.iterdir():
        if not year_dir.is_dir():
            continue
        candidate = year_dir / folder_name
        if candidate.is_dir():
            return candidate
    return None


def _import_ebl_folder(
    paths: List[Path], dest_root: Path, year: str, folder_name: str, report: Callable[[str, str], None],
) -> Tuple[int, int, List[str], List[dict]]:
    base_dir = dest_root / year / folder_name
    existing = _find_existing_ebl_folder(dest_root, folder_name)
    target_dir = base_dir
    renamed: List[str] = []
    if existing is not None:
        conflict = any(
            (existing / source.name).exists() and not _same_content(source, existing / source.name)
            for source in paths
        )
        if conflict:
            # One digest for the whole folder, from every source file's own name+content, sorted
            # so the result doesn't depend on the order the platform layer happened to list them
            # in.
            digest = hashlib.sha1(
                b"".join(sorted(source.name.encode("utf-8") + source.read_bytes() for source in paths))
            ).hexdigest()[:8]
            target_dir = base_dir.with_name(f"{base_dir.name}-{digest}")
            renamed.append(f"{folder_name} -> {target_dir.name}")
        else:
            # Same folder, not a collision: keep growing it exactly where it already lives,
            # rather than starting a second one under this batch's own year candidate (which can
            # legitimately differ run to run -- see _group_year's own doc comment).
            target_dir = existing

    target_dir.mkdir(parents=True, exist_ok=True)
    imported = 0
    duplicates = 0
    files: List[dict] = []
    for source in paths:
        dest = target_dir / source.name
        if dest.exists():
            if _same_content(source, dest):
                duplicates += 1
                files.append({"name": source.name, "outcome": "skipped_duplicate"})
                report(source.name, "skipped_duplicate")
                continue
            unique = _unique_path(dest)
            renamed.append(f"{source.name} -> {unique.name}")
            dest = unique
        shutil.copy2(source, dest)
        imported += 1
        files.append({"name": source.name, "outcome": "imported"})
        report(source.name, "imported")
    return imported, duplicates, renamed, files


def _import_loose_file(
    path: Path, year_dir: Path, report: Callable[[str, str], None],
) -> Tuple[int, int, List[str], List[dict]]:
    year_dir.mkdir(parents=True, exist_ok=True)
    dest = year_dir / path.name
    if dest.exists():
        if _same_content(path, dest):
            report(path.name, "skipped_duplicate")
            return 0, 1, [], [{"name": path.name, "outcome": "skipped_duplicate"}]
        unique = _unique_path(dest)
        renamed = [f"{path.name} -> {unique.name}"]
        dest = unique
    else:
        renamed = []
    shutil.copy2(path, dest)
    report(path.name, "imported")
    return 1, 0, renamed, [{"name": path.name, "outcome": "imported"}]


def _group_year(paths: List[Path]) -> str:
    """The earliest of the group's own resolvable timestamps -- not just any one of them -- so a
    session that happens to cross midnight on New Year's Eve still lands as a single folder under
    the year it started in, instead of splitting one real W2K-2 session in two."""
    timestamps = [t for t in (first_ebl_timestamp(path) for path in paths) if t is not None]
    return str(min(timestamps).year) if timestamps else _UNKNOWN_YEAR


def import_staged_ebl_files(
    staged_paths: List[str], dest_dir: str, progress_callback: Optional[ImportProgressCallback] = None,
) -> dict:
    """Imports every file in ``staged_paths`` (already copied onto local disk by the platform
    layer, see this module's own doc comment) into ``dest_dir`` (the app's own Actisense folder),
    each under ``<year>/<EBLnnnnnn or nothing>/<original filename>``.

    ``progress_callback``, if given, is called as ``progress_callback(current, total, name,
    outcome)`` right after each file's own outcome is decided, current/total 1-based -- the same
    "real work, then report it, one file at a time" shape a download's own SyncController.report()/
    ProgressCallback.report() already use, so a platform-side log/progress bar can show files
    landing one at a time as they actually happen instead of replaying an already-finished list
    once this function returns (see ImportProgressCallback's own doc comment for why that
    replay-after-the-fact shape doesn't work, tried and found wanting in practice).

    Returns ``{"imported": int, "skipped_duplicate": int, "renamed": [str, ...], "errors":
    [str, ...], "files": [{"name": str, "outcome": "imported" | "skipped_duplicate"}, ...]}`` --
    "files" is still built and returned in full, in case a caller wants the whole outcome list at
    once too (Android's own JSON-returning wrapper still reads counts off this dict, not off the
    live callback); progress_callback is purely an additional, live echo of the same information.

    "renamed" is every case where a *different* file already occupied the name this import
    wanted to use (a same-named file with different content -- the reformatted-SD-card scenario
    this module's own doc comment describes, or, rarer, a genuine name clash between two loose,
    non-EBLnnnnnn files), each as an ``"<original name> -> <name actually used>"`` string --
    worth its own, more alarming log line on the platform side (nothing was lost, but two same-
    named files turned out to hold different data, which is worth a second look).

    "files" is every source file's own outcome, in the order they were given, for a platform-side
    per-file log the same way a download already shows one -- asked for explicitly (a folder-wide
    rename above still gets only the one aggregate "renamed" line, not one per file it affected:
    every file in it already reads as a plain "imported" here, which is accurate on its own terms
    -- it *was* imported; which folder it landed in is the renamed line's own story to tell, once,
    not each file's).

    A per-file or per-folder failure (unreadable file, permission error) is recorded in "errors"
    and skipped rather than aborting the rest of the import.
    """
    dest_root = Path(dest_dir)
    imported = 0
    skipped_duplicate = 0
    renamed: List[str] = []
    errors: List[str] = []
    files: List[dict] = []

    total = len(staged_paths)
    processed = 0

    def report(name: str, outcome: str) -> None:
        nonlocal processed
        processed += 1
        if progress_callback is not None:
            progress_callback(processed, total, name, outcome)

    ebl_groups: Dict[str, List[Path]] = defaultdict(list)
    loose_files: List[Path] = []
    for raw in staged_paths:
        path = Path(raw)
        if EBL_FOLDER_NAME.match(path.parent.name) and EBL_FILE_NAME.match(path.name):
            ebl_groups[path.parent.name].append(path)
        else:
            loose_files.append(path)

    for folder_name, paths in ebl_groups.items():
        try:
            year = _group_year(paths)
            n_imported, n_dup, n_renamed, n_files = _import_ebl_folder(paths, dest_root, year, folder_name, report)
            imported += n_imported
            skipped_duplicate += n_dup
            renamed += n_renamed
            files += n_files
        except OSError as e:
            errors.append(f"{folder_name}: {e}")

    for path in loose_files:
        try:
            when = first_ebl_timestamp(path)
            year = str(when.year) if when is not None else _UNKNOWN_YEAR
            n_imported, n_dup, n_renamed, n_files = _import_loose_file(path, dest_root / year, report)
            imported += n_imported
            skipped_duplicate += n_dup
            renamed += n_renamed
            files += n_files
        except OSError as e:
            errors.append(f"{path.name}: {e}")

    return {
        "imported": imported,
        "skipped_duplicate": skipped_duplicate,
        "renamed": renamed,
        "errors": errors,
        "files": files,
    }
