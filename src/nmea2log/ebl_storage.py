"""The raw ``.ebl`` files kept on a phone: counting them, deleting them and copying them to a folder (the "Local .ebl files"
setting of the Android and iOS apps). The logic is here once, so both apps do exactly the same; the apps only show the result.
(The Android app cannot use ``backup_to``: the folder it copies to is a content:// tree, not a path, so it follows the same
rules in Kotlin: ``<folder>/Actisense/<the same sub-folders>/<name>``, a file already there with the same size is left alone.)"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .app_constants import EBL_DIR_NAME


def ebl_files(root: Path) -> List[Path]:
    """Every ``.ebl`` file under ``root`` (any depth, the extension in any case)."""
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() == ".ebl")


def format_size(size_bytes: int) -> str:
    """``size_bytes`` as "340 MB" / "1.2 GB": decimal units, one decimal from GB up, none below."""
    size = float(size_bytes)
    for unit in ("B", "KB", "MB"):
        if size < 1000:
            return f"{size:.0f} {unit}"
        size /= 1000
    return f"{size:.1f} GB" if size < 1000 else f"{size / 1000:.1f} TB"


def describe(root: Path) -> Tuple[int, str]:
    """(number of .ebl files, their total size as text) under ``root``."""
    files = ebl_files(root)
    return len(files), format_size(sum(path.stat().st_size for path in files))


def delete_all(root: Path) -> Tuple[int, str]:
    """Deletes every .ebl file under ``root`` and the folders that are empty afterwards (never ``root``
    itself). Returns what was deleted, like describe(). A file that cannot be deleted is left in place and
    not counted."""
    root = Path(root)
    deleted = 0
    freed = 0
    for path in ebl_files(root):
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError:
            continue
        deleted += 1
        freed += size
    if root.is_dir():
        for folder in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            try:
                folder.rmdir()
            except OSError:
                pass  # not empty: something else is in it
    return deleted, format_size(freed)



def backup_to(
    root: Path,
    destination: Path,
    progress: Optional[Callable[[int, int, str], None]] = None,
    folder_name: str = EBL_DIR_NAME,
) -> dict:
    """Copies every .ebl file under ``root`` to ``destination``/``folder_name``/..., keeping the folders they are in, so the
    copy is the same archive and can be imported again as it is. A file that is already there with the same size is left
    alone (a second copy only adds what is new). A file is first written under a temporary name and then renamed, so a stick
    pulled halfway never leaves a half file under the real name.

    ``progress(current, total, name)`` is called after each file. Returns ``{"copied": n, "skipped": n, "errors": [name, ...],
    "size": text}`` (``size`` is what was copied, like describe()). A file that cannot be copied (no room, the drive went away)
    is recorded and the rest is still tried."""
    root = Path(root)
    target_root = Path(destination) / folder_name
    files = ebl_files(root)
    copied = skipped = 0
    copied_bytes = 0
    errors: List[str] = []
    for index, source in enumerate(files, 1):
        target = target_root / source.relative_to(root)
        try:
            size = source.stat().st_size
            if target.is_file() and target.stat().st_size == size:
                skipped += 1
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + ".part")
                shutil.copyfile(source, temporary)
                os.replace(temporary, target)
                copied += 1
                copied_bytes += size
        except OSError:
            errors.append(source.name)
        if progress is not None:
            progress(index, len(files), source.name)
    return {"copied": copied, "skipped": skipped, "errors": errors, "size": format_size(copied_bytes)}
