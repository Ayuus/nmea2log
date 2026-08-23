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
        'De knoppen "Kaart", "Details" en "Opmerking" in dit logboek hebben JavaScript nodig om te '
        "openen. De meeste e-mailprogramma's verwijderen dat uit bijlagen -- open dit bestand in "
        "dat geval in een webbrowser (Chrome, Edge, Firefox, Safari, ...) in plaats van het "
        "rechtstreeks vanuit de e-mail te bekijken."
    ),
    # Trips table headers (see html_writer._HEADERS)
    "header_seq_abbr": "Nr.",
    "header_seq_full": "Volgnummer",
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
    "header_warnings": "Alarm",
    "header_water_temp": "Watertemperatuur",
    "header_motion": "Beweging",
    "header_route": "Route",
    "header_details": "Details",
    "header_remarks": "Opmerkingen",
    # Week divider row, e.g. "Week 32 (11 aug - 17 aug)"
    "week_label_prefix": "Week",
    # Totals cards
    "totals_trips": "Reizen",
    "totals_distance": "Totale afstand",
    "totals_hours": "Totale vaaruren",
    "totals_fuel_calculated": "Totale brandstof",
    "totals_fuel_engine_meter": "Totale brandstof (motormeter)",
    "totals_avg_consumption": "Gem. verbruik",
    "totals_avg_speed": "Gem. snelheid",
    "totals_top_speed": "Topsnelheid",
    "totals_engine_hour_meter": "Motoruren-teller",
    "totals_engine_hour_meter_engine": "Motoruren-teller, motor {instance}",
    "totals_hours_logged": "Gelogde motoruren",
    "totals_hours_logged_engine": "Gelogde motoruren, motor {instance}",
    # Motion (roll/pitch) column
    "motion_roll": "slingeren",
    "motion_pitch": "stampen",
    "motion_peak": "piek",
    # Typical RPM tooltip
    "rpm_tooltip_single": "gem. {avg} kn bij dat toerental ({min}-{max} kn)",
    "rpm_tooltip_single_fuel": "gem. {avg} kn bij dat toerental ({min}-{max} kn), gem. verbruik {fuel} L/nm",
    "rpm_tooltip_per_engine": "motor {instance}: gem. {avg} kn ({min}-{max} kn)",
    "rpm_tooltip_per_engine_fuel": "motor {instance}: gem. {avg} kn ({min}-{max} kn), gem. verbruik {fuel} L/nm",
    # Max speed tooltip
    "max_speed_tooltip_single": "om {time} bij {rpm} rpm",
    "max_speed_tooltip_per_engine": "om {time}, motor {instance}: {rpm} rpm",
    # Map button + popup
    "map_button_show": "Kaart",
    "map_marker_departure": "Vertrek",
    "map_marker_arrival": "Aankomst",
    # Details popup (water temperature, motion, periodic log)
    "details_button": "Details",
    "details_log_heading": "Log",
    "log_close_button": "Sluiten",
    "log_header_number": "Nr.",
    "log_header_time": "Tijd",
    "log_header_position": "Positie",
    "log_header_cog": "Koers",
    "log_header_sog": "Snelheid",
    # Remarks button + popup. Auth for both reading and saving rides on the WordPress login
    # session that got you to the (login-gated) logbook page in the first place -- no separate
    # username/password step here, see wordpress-plugin/little_endian-index.php.
    "remarks_button_placeholder": "Opmerking",
    "remarks_save_button": "Opslaan",
    "remarks_cancel_button": "Annuleren",
    # Shown instead of Opslaan/Annuleren for a logged-in visitor who can read but not save (see
    # the "can_edit" field the REST endpoint returns) -- just a way out of the dialog, since
    # there's nothing to cancel if nothing can be changed in the first place.
    "remarks_close_button": "Sluiten",
    "remarks_save_forbidden": "Je account mag geen opmerkingen opslaan.",
    "remarks_save_failed": "Opslaan is niet gelukt, probeer het opnieuw.",
    "remarks_unavailable": "Opmerkingen konden niet worden geladen.",
}

# strftime's %b is locale-independent (always English month abbreviations) unless the process
# locale is changed, which is fragile/platform-dependent -- a lookup table avoids that.
MONTH_ABBR_NL: Dict[str, str] = {
    "Jan": "jan", "Feb": "feb", "Mar": "mrt", "Apr": "apr", "May": "mei", "Jun": "jun",
    "Jul": "jul", "Aug": "aug", "Sep": "sep", "Oct": "okt", "Nov": "nov", "Dec": "dec",
}
