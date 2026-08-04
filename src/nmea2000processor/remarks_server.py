"""A tiny local web form for adding free-text remarks to trips (see ``remarks.py``), started with
``nmea2log-edit``. Plain standard-library ``http.server`` -- no external service, no internet
needed, nothing installed. Reads trip identities from ``trip_ids.json`` (written by ``nmea2log``
itself) so it can show a human-readable list without re-parsing any NMEA data.

Run ``nmea2log`` again after saving remarks here to see them merged into the CSV/HTML logbook.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from xml.sax.saxutils import escape

from . import remarks, trip_ids

DEFAULT_PORT = 8765


@dataclass
class _TripSummary:
    uid: str
    date: str
    depart_place: str
    arrive_place: str


def _load_trip_summaries(trip_ids_path: Path) -> List[_TripSummary]:
    if not trip_ids_path.exists():
        return []
    registry: Dict[str, str] = json.loads(trip_ids_path.read_text(encoding="utf-8"))
    summaries = []
    for key, uid in registry.items():
        date, depart_place, arrive_place, _occurrence = key.split("|", 3)
        summaries.append(_TripSummary(uid=uid, date=date, depart_place=depart_place, arrive_place=arrive_place))
    summaries.sort(key=lambda s: (s.date, s.depart_place, s.arrive_place))
    return summaries


def _parse_form_body(body: bytes) -> Dict[str, str]:
    """Each trip's textarea is named ``remark:{uid}`` -- the uid is embedded in the field name
    itself, so pairing a submitted value back to its trip doesn't depend on field order."""
    parsed = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
    result = {}
    for key, values in parsed.items():
        if key.startswith("remark:"):
            result[key[len("remark:") :]] = values[0]
    return result


def _render_page(summaries: List[_TripSummary], current_remarks: Dict[str, str]) -> str:
    if not summaries:
        body = (
            "<p>No trips found yet. Run <code>nmea2log</code> at least once first so it knows "
            "which trips exist.</p>"
        )
    else:
        rows = []
        for s in summaries:
            value = escape(current_remarks.get(s.uid, ""))
            rows.append(
                f"<tr><td>{escape(s.date)}</td>"
                f"<td>{escape(s.depart_place)} &rarr; {escape(s.arrive_place)}</td>"
                f'<td><textarea name="remark:{escape(s.uid)}" rows="2">{value}</textarea></td></tr>'
            )
        body = (
            '<form method="post" action="/save">'
            "<table><thead><tr><th>Date</th><th>Trip</th><th>Remark</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
            '<button type="submit">Save</button></form>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Edit trip remarks</title>
<style>
  body {{ font-family: sans-serif; margin: 0; padding: 1.5em; background: #f7f7f8; color: #1a1a1a; }}
  table {{ border-collapse: collapse; width: 100%; background: white; }}
  th, td {{ padding: 0.5em; border-bottom: 1px solid #eee; text-align: left; vertical-align: top; }}
  th {{ background: #f0f0f0; }}
  textarea {{ width: 100%; box-sizing: border-box; font: inherit; }}
  button {{ margin-top: 1em; padding: 0.6em 1.5em; background: #1a6ecc; color: white; border: none; border-radius: 4px; cursor: pointer; }}
  button:hover {{ background: #14589e; }}
</style>
</head>
<body>
<h1>Edit trip remarks</h1>
<p>Saves to remarks.csv. Re-run nmea2log afterwards to see remarks in the logbook.</p>
{body}
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    trip_ids_path: Path = trip_ids.DEFAULT_REGISTRY_PATH
    remarks_path: Path = remarks.DEFAULT_REMARKS_PATH

    def do_GET(self) -> None:  # noqa: N802 -- stdlib method name
        if self.path != "/":
            self.send_response(404)
            self.end_headers()
            return
        summaries = _load_trip_summaries(self.trip_ids_path)
        current_remarks = remarks.load_remarks(self.remarks_path)
        self._send_html(_render_page(summaries, current_remarks))

    def do_POST(self) -> None:  # noqa: N802 -- stdlib method name
        if self.path != "/save":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        submitted = _parse_form_body(self.rfile.read(length))

        summaries = _load_trip_summaries(self.trip_ids_path)
        labels = {s.uid: f"{s.date}|{s.depart_place}|{s.arrive_place}" for s in summaries}
        updated = {uid: text.strip() for uid, text in submitted.items() if text.strip()}
        remarks.save_remarks(updated, labels, self.remarks_path)

        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nmea2log-edit",
        description="Local web form for adding remarks to trips. Nothing here needs internet "
        "or an external service -- it's a plain local HTTP server serving only your machine.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Local port to serve on (default {DEFAULT_PORT})")
    parser.add_argument("--trip-ids-file", type=Path, default=trip_ids.DEFAULT_REGISTRY_PATH)
    parser.add_argument("--remarks-file", type=Path, default=remarks.DEFAULT_REMARKS_PATH)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if not args.trip_ids_file.exists():
        print(
            f"{args.trip_ids_file} not found -- run nmea2log at least once first so it knows "
            "which trips exist.",
            file=sys.stderr,
        )
        return 1

    _Handler.trip_ids_path = args.trip_ids_file
    _Handler.remarks_path = args.remarks_file

    server = HTTPServer(("127.0.0.1", args.port), _Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Serving remarks editor at {url} (Ctrl+C to stop)")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
