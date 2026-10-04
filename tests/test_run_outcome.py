import json

from nmea2log.app_texts import TEXTS
from nmea2log.run_outcome import (
    NO_EBL_FILES,
    SHOW_ERROR,
    SHOW_EXISTING_LOGBOOK,
    SHOW_LOG,
    SHOW_LOGBOOK,
    W2K2_NOT_FOUND,
    describe_result,
    describe_result_json,
    error_kind,
    publish_failed,
)


def test_a_successful_run_with_a_download_reports_trips_and_files_and_shows_the_logbook():
    outcome = describe_result({"ok": True, "trip_count": 25, "downloaded_count": 3}, "download")

    assert [(l.level, l.key, l.params) for l in outcome.lines] == [
        ("info", "status_ready_with_download", {"trips": 25, "files": 3})
    ]
    assert outcome.show == SHOW_LOGBOOK


def test_a_successful_assemble_has_no_download_count():
    outcome = describe_result({"ok": True, "trip_count": 25}, "build")

    assert outcome.lines[0].key == "status_ready_no_download"
    assert outcome.lines[0].params == {"trips": 25}


def test_a_success_without_a_trip_count_is_reported_as_updated():
    assert describe_result({"ok": True, "trip_count": None}, "download").lines[0].key == "status_ready_updated"
    assert describe_result({"ok": True, "trip_count": -1}, "download").lines[0].key == "status_ready_updated"


def test_a_failed_publish_keeps_the_log_in_front_of_the_new_logbook():
    outcome = describe_result({"ok": True, "trip_count": 2}, "build", publish_failed=True)

    assert outcome.show == SHOW_LOG


def test_a_cancelled_run_says_which_kind_was_stopped():
    assert describe_result({"ok": False, "cancelled": True}, "download").lines[0].key == "status_sync_stopped"
    assert describe_result({"ok": False, "cancelled": True}, "build").lines[0].key == "status_build_stopped"
    assert describe_result({"ok": False, "cancelled": True}, "build").show == SHOW_LOG


def test_no_w2k2_found_is_a_calm_line_and_the_existing_logbook():
    result = {"ok": False, "error": "No W2K-2 found on 192.168.1.0/24 -- is it joined to this hotspot?",
              "error_kind": W2K2_NOT_FOUND}

    outcome = describe_result(result, "download")

    assert outcome.lines[0].level == "info"
    assert outcome.lines[0].text == result["error"]
    assert outcome.show == SHOW_EXISTING_LOGBOOK


def test_no_ebl_files_is_a_friendly_line_not_an_error():
    outcome = describe_result({"ok": False, "error": "No .ebl files given.", "error_kind": NO_EBL_FILES}, "build")

    assert (outcome.lines[0].level, outcome.lines[0].key) == ("info", "log_no_ebl_files_to_build")
    assert outcome.show == SHOW_LOG


def test_a_result_without_error_kind_is_recognised_from_its_text():
    assert error_kind({"error": "No W2K-2 found on 10.0.0.0/24 -- x"}) == W2K2_NOT_FOUND
    assert error_kind({"error": "No .ebl files given."}) == NO_EBL_FILES
    assert error_kind({"error": "HTTP 401"}) is None


def test_any_other_error_is_an_error_line_with_the_message():
    outcome = describe_result({"ok": False, "error": "HTTP 401"}, "download")

    assert (outcome.lines[0].level, outcome.lines[0].key, outcome.lines[0].params) == (
        "error", "error_generic_prefix", {"error": "HTTP 401"})
    assert outcome.show == SHOW_ERROR


def test_publish_failed_only_counts_an_upload_that_was_attempted():
    assert publish_failed(published=False, rest_complete=True, sftp_complete=False) is True
    assert publish_failed(published=False, rest_complete=False, sftp_complete=True) is True
    assert publish_failed(published=False, rest_complete=False, sftp_complete=False) is False
    assert publish_failed(published=True, rest_complete=True, sftp_complete=False) is False


def test_every_key_it_returns_exists_in_the_shared_texts():
    results = [
        ({"ok": True, "trip_count": 1, "downloaded_count": 1}, "download"),
        ({"ok": True, "trip_count": 1}, "build"),
        ({"ok": True}, "build"),
        ({"ok": False, "cancelled": True}, "download"),
        ({"ok": False, "cancelled": True}, "build"),
        ({"ok": False, "error": "No .ebl files given."}, "build"),
        ({"ok": False, "error": "boom"}, "build"),
    ]
    for result, initiator in results:
        for line in describe_result(result, initiator).lines:
            assert line.key is None or line.key in TEXTS, line.key


def test_the_json_entry_point_matches_describe_result():
    answer = json.loads(
        describe_result_json(json.dumps({"ok": True, "trip_count": 4, "downloaded_count": 2}), "download", False)
    )

    assert answer == {
        "lines": [{"level": "info", "key": "status_ready_with_download", "params": {"trips": 4, "files": 2}, "text": None}],
        "show": SHOW_LOGBOOK,
    }


def test_an_import_reports_renamed_and_failed_files_as_warnings_then_a_summary():
    from nmea2log.run_outcome import describe_import

    lines = describe_import(
        {"imported": 3, "skipped_duplicate": 2, "renamed": ["EBL000001 -> EBL000001-1"], "errors": ["x.ebl: denied"]}
    )

    assert [(l.level, l.key, l.params) for l in lines] == [
        ("warning", "log_import_renamed", {"detail": "EBL000001 -> EBL000001-1"}),
        ("warning", "log_import_file_error", {"detail": "x.ebl: denied"}),
        ("", "log_import_done", {"imported": 3, "skipped": 2}),
    ]


def test_an_import_of_only_known_files_says_so():
    from nmea2log.run_outcome import describe_import

    lines = describe_import({"imported": 0, "skipped_duplicate": 5, "renamed": [], "errors": []})

    assert [(l.level, l.key, l.params) for l in lines] == [("info", "log_import_all_duplicates", {"count": 5})]


def test_an_import_that_found_nothing_says_so():
    from nmea2log.run_outcome import describe_import

    assert describe_import({"imported": 0, "skipped_duplicate": 0})[0].key == "log_import_no_files"


def test_the_import_keys_exist_in_the_shared_texts():
    from nmea2log.run_outcome import describe_import

    for result in ({"imported": 1, "skipped_duplicate": 0, "renamed": ["a"], "errors": ["b"]}, {"skipped_duplicate": 1}, {}):
        for line in describe_import(result):
            assert line.key in TEXTS, line.key
