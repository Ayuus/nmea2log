"""The help of the Android and iOS apps: one page for both (assets/help/help.html, in English, Dutch, French and German), kept on the phone
so it works at sea, and refreshed from GitHub when there is internet.

The text is shared; what differs per platform is hidden by the page's own css (``render`` sets the language and the platform
on the <html> element), and the screenshots, which each app brings along as ``img/<name>.png`` next to the page.

Order of preference for what an app shows: the copy it downloaded last (``<cache_dir>/help.html``), else the page that came
with the app. A download that is not a help page of the format this code understands (a different ``help-format``, too big, not
HTML) is thrown away, so a future page for newer apps cannot break an older one.
"""

from __future__ import annotations

import base64
import os
import re
import time
import urllib.error
import urllib.request
from importlib import resources
from pathlib import Path
from typing import Callable, Optional

from ._net import urlopen_ipv4_first
from .log import log

HELP_URL = "https://raw.githubusercontent.com/Ayuus/nmea2log/main/src/nmea2log/assets/help/help.html"

# The page says which format it is in (<meta name="help-format">); a page in another format is not for this code.
FORMAT_MARKER = '<meta name="help-format" content="1">'
MAX_BYTES = 1_000_000
CHECK_EVERY_S = 24 * 3600
TIMEOUT_S = 15.0

PLATFORMS = ("android", "ios")
LANGUAGES = ("en", "nl", "fr", "de")  # the languages the page has; any other language shows English

_CACHE_FILE = "help.html"
_ETAG_FILE = "help.etag"
_CHECKED_FILE = "help.checked"
_ROOT_TAG = '<html lang="en">'


def bundled_html() -> str:
    """The page that came with the app."""
    return resources.files("nmea2log").joinpath("assets", "help", "help.html").read_text(encoding="utf-8")


def is_valid(html: str) -> bool:
    """Whether ``html`` is a help page this code can use."""
    return FORMAT_MARKER in html and _ROOT_TAG in html and html.rstrip().endswith("</html>") and len(html) <= MAX_BYTES


def render(html: str, platform: str, language: str) -> str:
    """The page for one platform and language: ``<html lang="nl" class="ios">``. An unknown platform shows both platforms'
    notes; a language the page does not have shows English."""
    lang = language[:2].lower() if language and language[:2].lower() in LANGUAGES else "en"
    css_class = f' class="{platform}"' if platform in PLATFORMS else ""
    return html.replace(_ROOT_TAG, f'<html lang="{lang}"{css_class}>', 1)


def inline_images(html: str, images_dir: os.PathLike) -> str:
    """The page with its ``img/<name>.png`` images embedded as data, for a web view that cannot read files next to the page
    (iOS). An image that is missing is left as it is."""
    directory = Path(images_dir)

    def embed(match: "re.Match[str]") -> str:
        try:
            data = (directory / match.group(1)).read_bytes()
        except OSError:
            return match.group(0)
        return 'src="data:image/png;base64,' + base64.b64encode(data).decode("ascii") + '"'

    return re.sub(r'src="img/([\w.-]+\.png)"', embed, html)


def current_html(cache_dir: os.PathLike) -> str:
    """The newest page the app has: the downloaded copy, or the one that came with the app."""
    try:
        cached = (Path(cache_dir) / _CACHE_FILE).read_text(encoding="utf-8")
        if is_valid(cached):
            return cached
    except (OSError, ValueError):
        pass
    return bundled_html()


def html_for_app(cache_dir: os.PathLike, platform: str, language: str) -> str:
    """What the app shows: the newest page, for this platform and language."""
    return render(current_html(cache_dir), platform, language)


def _write_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def refresh(
    cache_dir: os.PathLike,
    now: Optional[float] = None,
    force: bool = False,
    opener: Callable[[urllib.request.Request, float], object] = urlopen_ipv4_first,
) -> bool:
    """Fetches the help page from GitHub when it has changed (at most once per ``CHECK_EVERY_S`` unless ``force``) and keeps
    it in ``cache_dir``. True when a newer page was stored. Never raises: no internet, a server error or a page that is not
    valid just leaves what the app had."""
    directory = Path(cache_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        clock = time.time() if now is None else now
        checked_file = directory / _CHECKED_FILE
        if not force:
            try:
                if clock - float(checked_file.read_text()) < CHECK_EVERY_S:
                    return False
            except (OSError, ValueError):
                pass
        headers = {"User-Agent": "nmea2log-help"}
        cache_file = directory / _CACHE_FILE
        etag_file = directory / _ETAG_FILE
        if cache_file.exists() and etag_file.exists():
            headers["If-None-Match"] = etag_file.read_text().strip()
        try:
            response = opener(urllib.request.Request(HELP_URL, headers=headers), TIMEOUT_S)
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                _write_atomic(checked_file, str(clock))
            return False
        with response:  # type: ignore[attr-defined]
            body = response.read(MAX_BYTES + 1)  # type: ignore[attr-defined]
            etag = response.headers.get("ETag", "")  # type: ignore[attr-defined]
        _write_atomic(checked_file, str(clock))
        html = body.decode("utf-8")
        if not is_valid(html):
            log("[info] The help page on GitHub is not one this app can use; keeping the one it has.")
            return False
        if cache_file.exists() and cache_file.read_text(encoding="utf-8") == html:
            return False
        _write_atomic(cache_file, html)
        if etag:
            _write_atomic(etag_file, etag)
        else:
            etag_file.unlink(missing_ok=True)
        return True
    except Exception as exc:  # the help is a nicety: whatever goes wrong, the app goes on with the page it has
        log(f"[debug] Could not refresh the help page: {exc}")
        return False
