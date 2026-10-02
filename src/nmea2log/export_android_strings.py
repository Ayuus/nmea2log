"""Writes the shared texts (see app_texts.py) into the Android app's ``strings.xml`` files.

    python -m nmea2log.export_android_strings <res dir> [--check]

Only ``<string>`` elements that already exist in ``values/``, ``values-nl/``, ``values-fr/`` and
``values-de/`` are touched, and only their text; everything else in the files stays as it is. With
``--check`` nothing is written and the exit status says whether the files are already in sync."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List

from .app_texts import TEXTS

_LANG_DIRS = {"en": "values", "nl": "values-nl", "fr": "values-fr", "de": "values-de"}
_NAMED = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_POSITIONAL = re.compile(r"%\d+\$([sd])")


def placeholder_names(text: str) -> List[str]:
    """The placeholder names of ``text`` in order of first appearance."""
    names: List[str] = []
    for name in _NAMED.findall(text):
        if name not in names:
            names.append(name)
    return names


def to_android(text: str, names: List[str], specs: List[str]) -> str:
    """``text`` as it is written in strings.xml: positional ``%N$s``/``%N$d`` arguments (``%`` doubled when
    there are any), XML entities, and the backslash escapes Android resources need."""
    value = text
    if names:
        value = value.replace("%", "%%")
    for index, name in enumerate(names):
        value = value.replace("{" + name + "}", f"%{index + 1}${specs[index]}")
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return value.replace("'", "\\'").replace('"', "\\\"").replace("\n", "\\n")


def _specs(current_english_value: str, count: int, key: str) -> List[str]:
    specs = [match.group(1) for match in _POSITIONAL.finditer(current_english_value)]
    if len(specs) != count:
        raise ValueError(f"{key}: {count} placeholder(s) in the shared text, {len(specs)} in the Android string")
    return specs


def export(res_dir: Path, check: bool = False) -> Dict[str, List[str]]:
    """Returns, per language, the keys whose Android text differed from the shared one (and were
    rewritten, unless ``check``)."""
    files = {lang: res_dir / folder / "strings.xml" for lang, folder in _LANG_DIRS.items()}
    contents = {lang: path.read_text(encoding="utf-8") for lang, path in files.items()}
    changed: Dict[str, List[str]] = {lang: [] for lang in files}

    for key, per_language in TEXTS.items():
        names = placeholder_names(per_language["en"])
        element = re.compile(r'(<string name="' + re.escape(key) + r'"[^>]*>)(.*?)(</string>)', re.S)
        english = element.search(contents["en"])
        if english is None:
            continue
        specs = _specs(english.group(2), len(names), key)
        for lang in files:
            if placeholder_names(per_language[lang]) != names and sorted(placeholder_names(per_language[lang])) != sorted(names):
                raise ValueError(f"{key} ({lang}): placeholders differ from the English text")
            match = element.search(contents[lang])
            if match is None:
                continue
            wanted = to_android(per_language[lang], names, specs)
            if match.group(2) != wanted:
                changed[lang].append(key)
                contents[lang] = contents[lang][: match.start(2)] + wanted + contents[lang][match.end(2):]

    if not check:
        for lang, path in files.items():
            if changed[lang]:
                path.write_text(contents[lang], encoding="utf-8", newline="")
    return changed


def main(argv: List[str]) -> int:
    check = "--check" in argv
    paths = [a for a in argv if not a.startswith("--")]
    if len(paths) != 1:
        print(__doc__)
        return 2
    changed = export(Path(paths[0]), check=check)
    total = sum(len(keys) for keys in changed.values())
    for lang, keys in changed.items():
        for key in keys:
            print(f"{'differs' if check else 'updated'}: {_LANG_DIRS[lang]}/{key}")
    print(f"{total} string(s) {'differ from' if check else 'updated from'} the shared texts.")
    return 1 if (check and total) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
