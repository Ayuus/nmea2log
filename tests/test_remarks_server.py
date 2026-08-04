import json
from pathlib import Path

from nmea2000processor.remarks_server import _TripSummary, _load_trip_summaries, _parse_form_body, _render_page


def test_load_trip_summaries_missing_file_returns_empty_list(tmp_path: Path):
    assert _load_trip_summaries(tmp_path / "does_not_exist.json") == []


def test_load_trip_summaries_parses_registry_and_sorts(tmp_path: Path):
    path = tmp_path / "trip_ids.json"
    path.write_text(
        json.dumps(
            {
                "2026-07-31|Loctudy|Sainte-Marine|0": "uid-2",
                "2026-07-30|Concarneau|Loctudy|0": "uid-1",
            }
        ),
        encoding="utf-8",
    )

    summaries = _load_trip_summaries(path)

    assert [s.uid for s in summaries] == ["uid-1", "uid-2"]  # sorted by date
    assert summaries[0].depart_place == "Concarneau"
    assert summaries[0].arrive_place == "Loctudy"


def test_parse_form_body_extracts_remarks_by_uid():
    body = b"remark:uid-1=Great+sail&remark:uid-2=&other=ignored"

    result = _parse_form_body(body)

    assert result == {"uid-1": "Great sail", "uid-2": ""}


def test_render_page_prefills_existing_remark_and_embeds_uid():
    summaries = [_TripSummary(uid="uid-1", date="2026-07-30", depart_place="Concarneau", arrive_place="Loctudy")]

    html = _render_page(summaries, {"uid-1": "Nice broad reach"})

    assert 'name="remark:uid-1"' in html
    assert "Nice broad reach" in html
    assert "Concarneau" in html and "Loctudy" in html


def test_render_page_without_trips_shows_hint():
    html = _render_page([], {})

    assert "nmea2log" in html
    assert "<form" not in html


def test_render_page_escapes_html_in_remark():
    summaries = [_TripSummary(uid="uid-1", date="2026-07-30", depart_place="A", arrive_place="B")]

    html = _render_page(summaries, {"uid-1": "<script>alert(1)</script>"})

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
