import os
import string
from pathlib import Path

import pytest

from nmea2log.app_texts import TEXTS
from nmea2log.export_android_strings import export, main, placeholder_names, to_android

LANGUAGES = ("en", "nl", "fr", "de")


def test_every_text_exists_in_all_four_languages():
    for key, per_language in TEXTS.items():
        assert set(per_language) == set(LANGUAGES), key
        assert all(text.strip() for text in per_language.values()), key


def test_a_text_uses_the_same_placeholders_in_every_language():
    for key, per_language in TEXTS.items():
        english = set(placeholder_names(per_language["en"]))
        for language, text in per_language.items():
            assert set(placeholder_names(text)) == english, (key, language)


def test_every_text_formats_with_its_placeholders():
    for key, per_language in TEXTS.items():
        for language, text in per_language.items():
            values = {name: "x" for name in placeholder_names(text)}
            assert "{" not in text.format(**values), (key, language)


def test_placeholder_names_follow_first_appearance():
    assert placeholder_names("{b} then {a} then {b}") == ["b", "a"]


def test_to_android_numbers_placeholders_and_escapes_the_xml_and_resource_characters():
    text = 'x {count} file' + chr(39) + 's "q" & <y> 100%' + chr(10) + 'next {name}'
    expected = r'x %1$d file' + chr(92) + chr(39) + r's \"q\" &amp; &lt;y&gt; 100%%' + chr(92) + 'nnext %2$s'
    assert to_android(text, ['count', 'name'], ['d', 's']) == expected


def test_to_android_leaves_percent_alone_without_placeholders():
    assert to_android("100% done", [], []) == "100% done"


_SAMPLE_KEY = "log_import_done"  # two numeric placeholders, in every language


def _write_res(res: Path, wording: dict) -> None:
    for language, folder in {"en": "values", "nl": "values-nl", "fr": "values-fr", "de": "values-de"}.items():
        (res / folder).mkdir(parents=True)
        (res / folder / "strings.xml").write_text(
            "<resources>\n"
            f'    <string name="{_SAMPLE_KEY}">{wording[language]}</string>\n'
            '    <string name="only_on_android">Untouched</string>\n'
            "</resources>\n",
            encoding="utf-8",
        )


def test_export_rewrites_a_differing_text_and_nothing_else(tmp_path):
    _write_res(tmp_path, {language: "old %1$d / %2$d" for language in LANGUAGES})

    changed = export(tmp_path)

    assert all(changed[language] == [_SAMPLE_KEY] for language in LANGUAGES)
    english = (tmp_path / "values" / "strings.xml").read_text(encoding="utf-8")
    assert to_android(TEXTS[_SAMPLE_KEY]["en"], ["imported", "skipped"], ["d", "d"]) in english
    assert '<string name="only_on_android">Untouched</string>' in english
    assert export(tmp_path) == {language: [] for language in LANGUAGES}  # idempotent


def test_export_check_writes_nothing_and_reports_the_difference(tmp_path, capsys):
    _write_res(tmp_path, {language: "old %1$d / %2$d" for language in LANGUAGES})
    before = (tmp_path / "values" / "strings.xml").read_text(encoding="utf-8")

    assert main([str(tmp_path), "--check"]) == 1

    assert (tmp_path / "values" / "strings.xml").read_text(encoding="utf-8") == before
    assert "differs: values/" + _SAMPLE_KEY in capsys.readouterr().out


@pytest.mark.skipif(
    "MYSAILINGLOGBOOK_ANDROID_RES" not in os.environ,
    reason="set MYSAILINGLOGBOOK_ANDROID_RES to the Android app's app/src/main/res to check it for drift",
)
def test_the_android_app_strings_match_the_shared_texts():
    changed = export(Path(os.environ["MYSAILINGLOGBOOK_ANDROID_RES"]), check=True)
    assert changed == {language: [] for language in LANGUAGES}
