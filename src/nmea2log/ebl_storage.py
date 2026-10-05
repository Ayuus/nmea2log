"""The raw ``.ebl`` files kept on a phone: counting them and deleting them (the "Local .ebl files" setting of the
Android and iOS apps). The logic is here once, so both apps do exactly the same; the apps only show the result."""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple


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
