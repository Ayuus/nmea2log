import io
import re
import urllib.error
from email.message import Message

import pytest

from nmea2log import help_page


class _Response:
    def __init__(self, body: bytes, etag: str = '"v1"') -> None:
        self._body = io.BytesIO(body)
        self.headers = {"ETag": etag} if etag else {}

    def read(self, n: int = -1) -> bytes:
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(body: bytes = b"", etag: str = '"v1"', error: Exception = None):
    calls = []

    def opener(request, timeout):
        calls.append(request)
        if error is not None:
            raise error
        return _Response(body, etag)

    opener.calls = calls
    return opener


def _page(extra: str = "") -> str:
    return help_page.bundled_html().replace("</body>", extra + "</body>")


def test_bundled_page_is_valid():
    assert help_page.is_valid(help_page.bundled_html())


def test_bundled_page_links_point_to_sections_and_images_are_the_known_ones():
    html = help_page.bundled_html()
    ids = set(re.findall(r'<section id="([\w-]+)"', html))
    for target in re.findall(r'href="#([\w-]+)"', html):
        assert target in ids, target
    # The images come with each app, under these names.
    assert set(re.findall(r'src="img/([\w-]+\.png)"', html)) == {"settings.png", "logbook.png", "map-trip.png", "boat-mode.png"}


def test_bundled_page_has_every_text_in_both_languages():
    html = help_page.bundled_html()
    # Every block in one language has its counterpart: the number of English and Dutch blocks per tag is the same.
    for tag in ("h1", "h2", "p", "dl", "figcaption", "span"):
        counts = {lang: len(re.findall(rf'<{tag}\b[^>]*lang="{lang}"', html)) for lang in help_page.LANGUAGES}
        assert len(set(counts.values())) == 1, (tag, counts)


def test_render_sets_language_and_platform():
    page = help_page.render(help_page.bundled_html(), "ios", "nl-NL")

    assert '<html lang="nl" class="ios">' in page


def test_render_knows_all_four_languages():
    for language in ("en", "nl", "fr", "de"):
        assert f'<html lang="{language}" class="ios">' in help_page.render(help_page.bundled_html(), "ios", language)


def test_render_shows_english_for_a_language_the_page_does_not_have():
    assert '<html lang="en" class="android">' in help_page.render(help_page.bundled_html(), "android", "es")
    assert '<html lang="en" class="android">' in help_page.render(help_page.bundled_html(), "android", "")


def test_render_without_a_known_platform_has_no_platform_class():
    assert '<html lang="en">' in help_page.render(help_page.bundled_html(), "windows", "en")


def test_current_html_is_the_bundled_page_without_a_cache(tmp_path):
    assert help_page.current_html(tmp_path) == help_page.bundled_html()


def test_current_html_ignores_a_cached_page_that_is_not_valid(tmp_path):
    (tmp_path / "help.html").write_text("<html>nope</html>", encoding="utf-8")

    assert help_page.current_html(tmp_path) == help_page.bundled_html()


def test_refresh_stores_a_newer_page_and_the_app_then_shows_it(tmp_path):
    newer = _page("<!-- newer -->")
    opener = _opener(newer.encode("utf-8"))

    assert help_page.refresh(tmp_path, now=1000.0, opener=opener) is True

    assert help_page.current_html(tmp_path) == newer
    assert (tmp_path / "help.etag").read_text() == '"v1"'


def test_refresh_checks_at_most_once_a_day(tmp_path):
    opener = _opener(_page("<!-- a -->").encode("utf-8"))
    help_page.refresh(tmp_path, now=1000.0, opener=opener)

    assert help_page.refresh(tmp_path, now=1000.0 + 3600, opener=opener) is False
    assert len(opener.calls) == 1

    assert help_page.refresh(tmp_path, now=1000.0 + 25 * 3600, opener=_opener(_page("<!-- b -->").encode("utf-8"))) is True


def test_refresh_force_ignores_the_daily_limit(tmp_path):
    help_page.refresh(tmp_path, now=1000.0, opener=_opener(_page("<!-- a -->").encode("utf-8")))
    opener = _opener(_page("<!-- b -->").encode("utf-8"), etag='"v2"')

    assert help_page.refresh(tmp_path, now=1001.0, force=True, opener=opener) is True


def test_refresh_asks_only_for_what_changed(tmp_path):
    help_page.refresh(tmp_path, now=1000.0, opener=_opener(_page("<!-- a -->").encode("utf-8")))
    opener = _opener(error=urllib.error.HTTPError(help_page.HELP_URL, 304, "Not Modified", Message(), None))

    assert help_page.refresh(tmp_path, now=1000.0 + 90000, opener=opener) is False

    assert opener.calls[0].get_header("If-none-match") == '"v1"'
    assert help_page.current_html(tmp_path) == _page("<!-- a -->")


def test_refresh_keeps_the_old_page_when_the_download_is_not_a_help_page(tmp_path):
    for body in (b"<html>an error page</html>", b"\xff\xfe not utf8", b"x" * (help_page.MAX_BYTES + 10)):
        assert help_page.refresh(tmp_path, now=1.0, force=True, opener=_opener(body)) is False

    assert help_page.current_html(tmp_path) == help_page.bundled_html()


def test_refresh_rejects_a_page_in_another_format(tmp_path):
    other = help_page.bundled_html().replace('content="1"', 'content="2"')

    assert help_page.refresh(tmp_path, now=1.0, opener=_opener(other.encode("utf-8"))) is False


def test_refresh_never_raises_without_internet(tmp_path):
    assert help_page.refresh(tmp_path, now=1.0, opener=_opener(error=OSError("Network is unreachable"))) is False
    assert help_page.refresh(tmp_path, now=1.0, opener=_opener(error=urllib.error.URLError("no route"))) is False
    assert help_page.refresh(tmp_path, now=1.0, opener=_opener(error=urllib.error.HTTPError(help_page.HELP_URL, 404, "Not Found", Message(), None))) is False


def test_a_failed_check_does_not_use_up_the_day(tmp_path):
    help_page.refresh(tmp_path, now=1000.0, opener=_opener(error=OSError("offline")))

    assert help_page.refresh(tmp_path, now=1001.0, opener=_opener(_page("<!-- a -->").encode("utf-8"))) is True


def test_an_unchanged_page_is_not_a_change(tmp_path):
    same = help_page.bundled_html().encode("utf-8")
    help_page.refresh(tmp_path, now=1.0, opener=_opener(same))

    assert help_page.refresh(tmp_path, now=1.0, force=True, opener=_opener(same)) is False


def test_inline_images_embeds_the_images_that_exist(tmp_path):
    (tmp_path / "settings.png").write_bytes(b"\x89PNG-fake")
    html = '<img src="img/settings.png" alt=""><img src="img/missing.png" alt="">'

    result = help_page.inline_images(html, tmp_path)

    assert 'src="data:image/png;base64,' in result
    assert 'src="img/missing.png"' in result
    assert "img/settings.png" not in result
