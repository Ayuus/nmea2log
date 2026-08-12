"""UI text for the generated HTML logbook, kept separate from the rendering logic in
html_writer.py so wording tweaks -- or a future second language -- don't need to touch the HTML-
building code itself. CSV/GPX output is deliberately not covered here: those stay English
regardless (see html_writer.py's module docstring), so a fixed script reading the CSV keeps
working no matter what language the HTML is in.

To add another language later: copy ``NL`` into a same-shaped dict (every key must be present --
html_writer.py doesn't fall back key-by-key), then wire a way to pick which one gets used.
"""

from __future__ import annotations

from typing import Dict

NL: Dict[str, str] = {
    # Page title / heading
    "logbook_title_suffix": "Vaarlogboek",
    "last_updated": "Laatst bijgewerkt",
    "vessel_call_sign": "Roepnaam",
    # <noscript> banner
    "noscript_warning": (
        'De knoppen "Kaart" en "Log" in dit logboek hebben JavaScript nodig om te openen. De '
        "meeste e-mailprogramma's verwijderen dat uit bijlagen -- open dit bestand in dat geval "
        "in een webbrowser (Chrome, Edge, Firefox, Safari, ...) in plaats van het rechtstreeks "
        "vanuit de e-mail te bekijken."
    ),
    # Trips table headers (see html_writer._HEADERS)
    "header_seq": "Volgnummer",
    "header_date": "Datum",
    "header_departure_abbr": "Vertr.",
    "header_departure_full": "Vertrek",
    "header_from": "Van",
    "header_arrival_abbr": "Aank.",
    "header_arrival_full": "Aankomst",
    "header_to": "Naar",
    "header_duration": "Duur",
    "header_distance": "Afstand",
    "header_avg_speed": "Gem. snelheid",
    "header_max_speed": "Max snelheid",
    "header_fuel": "Brandstof",
    "header_l_per_nm": "L/nm",
    "header_engine_hours": "Motoruren",
    "header_rpm": "Toerental",
    "header_warnings": "Waarschuwingen",
    "header_water_temp": "Watertemp.",
    "header_motion": "Beweging",
    "header_motion_tooltip": "slingeren (roll), stampen (pitch)",
    "header_route": "Route",
    "header_log": "Log",
    # Week divider row, e.g. "Week 32 (11 aug - 17 aug)"
    "week_label_prefix": "Week",
    # Totals cards
    "totals_trips": "Reizen",
    "totals_distance": "Totale afstand",
    "totals_hours": "Totale uren",
    "totals_fuel_calculated": "Totale brandstof (berekend)",
    "totals_fuel_engine_meter": "Totale brandstof (motormeter)",
    "totals_avg_consumption": "Gem. verbruik",
    "totals_avg_speed": "Gem. snelheid",
    "totals_top_speed": "Topsnelheid",
    "totals_engine_hour_meter": "Motoruren-teller",
    "totals_engine_hour_meter_engine": "Motoruren-teller, motor {instance}",
    "totals_hours_logged": "Gelogde uren",
    "totals_hours_logged_engine": "Gelogde uren, motor {instance}",
    # Motion (roll/pitch) column
    "motion_roll": "slingeren",
    "motion_pitch": "stampen",
    "motion_peak": "piek",
    # Typical RPM tooltip
    "rpm_tooltip_single": "gem. {avg} kn bij dat toerental ({min}-{max} kn)",
    "rpm_tooltip_per_engine": "motor {instance}: gem. {avg} kn ({min}-{max} kn)",
    # Map button + popup
    "map_button_show": "Kaart",
    "map_button_hide": "Kaart verbergen",
    "map_marker_departure": "Vertrek",
    "map_marker_arrival": "Aankomst",
    # Periodic log button + popup
    "log_button": "Log",
    "log_close_button": "Sluiten",
    "log_header_time": "Tijd",
    "log_header_position": "Positie",
    "log_header_cog": "Koers",
    "log_header_sog": "Snelheid",
}

# strftime's %b is locale-independent (always English month abbreviations) unless the process
# locale is changed, which is fragile/platform-dependent -- a lookup table avoids that.
MONTH_ABBR_NL: Dict[str, str] = {
    "Jan": "jan", "Feb": "feb", "Mar": "mrt", "Apr": "apr", "May": "mei", "Jun": "jun",
    "Jul": "jul", "Aug": "aug", "Sep": "sep", "Oct": "okt", "Nov": "nov", "Dec": "dec",
}
