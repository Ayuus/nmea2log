"""UI text for the generated HTML logbook, kept separate from the rendering logic in
html_writer.py so wording tweaks -- or another language -- don't need to touch the HTML-building
code itself. CSV/GPX output is deliberately not covered here: those stay English regardless (see
html_writer.py's module docstring), so a fixed script reading the CSV keeps working no matter what
language the HTML is in.

The generated page itself only ever renders in NL server-side (unchanged from before): every
translatable element also carries a data-i18n/data-i18n-tpl attribute (see html_writer.py), and all
four languages below are embedded in the page as one JS object so a visitor can switch languages
client-side without regenerating the file -- see the language-switcher button in the page header.
Free-form data that isn't UI chrome (place names, fault/warning text from the decoded NMEA data
itself) is never translated by this mechanism.

To add another language: copy ``NL`` into a same-shaped dict (every key must be present --
html_writer.py doesn't fall back key-by-key), add its month-abbreviation table, and register both
in ``LANGUAGES``/``MONTH_ABBR``/``LANGUAGE_FLAGS`` below.
"""

from __future__ import annotations

from typing import Dict

NL: Dict[str, str] = {
    # Page title / heading
    "logbook_title_suffix": "Vaarlogboek",
    "last_updated": "Laatst bijgewerkt",
    "last_position": "Laatste positie",
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
    "header_water_temp": "Watertemperatuur",
    "header_motion": "Beweging",
    "header_route": "Route",
    "header_details": "Log",
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
    "map_marker_max_speed": "Topsnelheid",
    # Details popup (water temperature, motion, periodic log)
    "details_button": "Log",
    "log_close_button": "Sluiten",
    "log_header_number": "Nr.",
    "log_header_time": "Tijd",
    "log_header_position": "Positie",
    "log_header_cog": "Koers",
    "log_header_sog": "Snelheid",
    "log_header_wind": "Wind",
    "log_header_precip": "Neerslag",
    "log_header_cloud": "Bewolking",
    "log_header_wave": "Golven",
    "log_header_current": "Stroming",
    # Remarks button + popup. Auth for both reading and saving rides on the WordPress login
    # session that got you to the (login-gated) logbook page in the first place -- no separate
    # username/password step here, see wordpress-plugin/logboek-index.php.
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

EN: Dict[str, str] = {
    "logbook_title_suffix": "Sailing Logbook",
    "last_updated": "Last updated",
    "last_position": "Last position",
    "vessel_call_sign": "Call sign",
    "noscript_warning": (
        'The "Map", "Details" and "Remark" buttons in this logbook need JavaScript to open. Most '
        "email clients strip that from attachments -- if that's how you're viewing this, open the "
        "file in a real web browser (Chrome, Edge, Firefox, Safari, ...) instead of viewing it "
        "straight from the email."
    ),
    "header_seq_abbr": "No.",
    "header_seq_full": "Sequence",
    "header_date": "Date",
    "header_departure_abbr": "Dep.",
    "header_departure_full": "Departure",
    "header_from": "From",
    "header_arrival_abbr": "Arr.",
    "header_arrival_full": "Arrival",
    "header_to": "To",
    "header_duration": "Duration",
    "header_distance": "Distance",
    "header_avg_speed": "Avg. speed",
    "header_max_speed": "Max speed",
    "header_fuel": "Fuel",
    "header_l_per_nm": "L/nm",
    "header_engine_hours": "Engine hours",
    "header_rpm": "RPM",
    "header_water_temp": "Water temperature",
    "header_motion": "Motion",
    "header_route": "Route",
    "header_details": "Log",
    "header_remarks": "Remarks",
    "week_label_prefix": "Week",
    "totals_trips": "Trips",
    "totals_distance": "Total distance",
    "totals_hours": "Total hours underway",
    "totals_fuel_calculated": "Total fuel",
    "totals_fuel_engine_meter": "Total fuel (engine meter)",
    "totals_avg_consumption": "Avg. consumption",
    "totals_avg_speed": "Avg. speed",
    "totals_top_speed": "Top speed",
    "totals_engine_hour_meter": "Engine hour meter",
    "totals_engine_hour_meter_engine": "Engine hour meter, engine {instance}",
    "totals_hours_logged": "Logged engine hours",
    "totals_hours_logged_engine": "Logged engine hours, engine {instance}",
    "motion_roll": "roll",
    "motion_pitch": "pitch",
    "motion_peak": "peak",
    "rpm_tooltip_single": "avg. {avg} kn at that RPM ({min}-{max} kn)",
    "rpm_tooltip_single_fuel": "avg. {avg} kn at that RPM ({min}-{max} kn), avg. consumption {fuel} L/nm",
    "rpm_tooltip_per_engine": "engine {instance}: avg. {avg} kn ({min}-{max} kn)",
    "rpm_tooltip_per_engine_fuel": "engine {instance}: avg. {avg} kn ({min}-{max} kn), avg. consumption {fuel} L/nm",
    "max_speed_tooltip_single": "at {time} at {rpm} rpm",
    "max_speed_tooltip_per_engine": "at {time}, engine {instance}: {rpm} rpm",
    "map_button_show": "Map",
    "map_marker_departure": "Departure",
    "map_marker_arrival": "Arrival",
    "map_marker_max_speed": "Top speed",
    "details_button": "Log",
    "log_close_button": "Close",
    "log_header_number": "No.",
    "log_header_time": "Time",
    "log_header_position": "Position",
    "log_header_cog": "Course",
    "log_header_sog": "Speed",
    "log_header_wind": "Wind",
    "log_header_precip": "Precip.",
    "log_header_cloud": "Cloud",
    "log_header_wave": "Waves",
    "log_header_current": "Current",
    "remarks_button_placeholder": "Remark",
    "remarks_save_button": "Save",
    "remarks_cancel_button": "Cancel",
    "remarks_close_button": "Close",
    "remarks_save_forbidden": "Your account isn't allowed to save remarks.",
    "remarks_save_failed": "Saving failed, please try again.",
    "remarks_unavailable": "Remarks could not be loaded.",
}

FR: Dict[str, str] = {
    "logbook_title_suffix": "Journal de bord",
    "last_updated": "Dernière mise à jour",
    "last_position": "Dernière position",
    "vessel_call_sign": "Indicatif d'appel",
    "noscript_warning": (
        'Les boutons « Carte », « Détails » et « Remarque » de ce journal de bord nécessitent '
        "JavaScript pour s'ouvrir. La plupart des messageries suppriment cela des pièces jointes -- "
        "si c'est le cas ici, ouvrez ce fichier dans un vrai navigateur (Chrome, Edge, Firefox, "
        "Safari, ...) plutôt que de le consulter directement depuis l'e-mail."
    ),
    "header_seq_abbr": "N°",
    "header_seq_full": "Numéro",
    "header_date": "Date",
    "header_departure_abbr": "Dép.",
    "header_departure_full": "Départ",
    "header_from": "De",
    "header_arrival_abbr": "Arr.",
    "header_arrival_full": "Arrivée",
    "header_to": "À",
    "header_duration": "Durée",
    "header_distance": "Distance",
    "header_avg_speed": "Vit. moy.",
    "header_max_speed": "Vit. max",
    "header_fuel": "Carburant",
    "header_l_per_nm": "L/nm",
    "header_engine_hours": "Heures moteur",
    "header_rpm": "Régime",
    "header_water_temp": "Température de l'eau",
    "header_motion": "Mouvement",
    "header_route": "Trajet",
    "header_details": "Journal",
    "header_remarks": "Remarques",
    "week_label_prefix": "Semaine",
    "totals_trips": "Trajets",
    "totals_distance": "Distance totale",
    "totals_hours": "Heures de navigation totales",
    "totals_fuel_calculated": "Carburant total",
    "totals_fuel_engine_meter": "Carburant total (compteur moteur)",
    "totals_avg_consumption": "Consommation moy.",
    "totals_avg_speed": "Vitesse moy.",
    "totals_top_speed": "Vitesse max",
    "totals_engine_hour_meter": "Compteur horaire moteur",
    "totals_engine_hour_meter_engine": "Compteur horaire moteur, moteur {instance}",
    "totals_hours_logged": "Heures moteur enregistrées",
    "totals_hours_logged_engine": "Heures moteur enregistrées, moteur {instance}",
    "motion_roll": "roulis",
    "motion_pitch": "tangage",
    "motion_peak": "pic",
    "rpm_tooltip_single": "moy. {avg} nd à ce régime ({min}-{max} nd)",
    "rpm_tooltip_single_fuel": "moy. {avg} nd à ce régime ({min}-{max} nd), conso. moy. {fuel} L/nm",
    "rpm_tooltip_per_engine": "moteur {instance} : moy. {avg} nd ({min}-{max} nd)",
    "rpm_tooltip_per_engine_fuel": "moteur {instance} : moy. {avg} nd ({min}-{max} nd), conso. moy. {fuel} L/nm",
    "max_speed_tooltip_single": "à {time} à {rpm} tr/min",
    "max_speed_tooltip_per_engine": "à {time}, moteur {instance} : {rpm} tr/min",
    "map_button_show": "Carte",
    "map_marker_departure": "Départ",
    "map_marker_arrival": "Arrivée",
    "map_marker_max_speed": "Vitesse max.",
    "details_button": "Journal",
    "log_close_button": "Fermer",
    "log_header_number": "N°",
    "log_header_time": "Heure",
    "log_header_position": "Position",
    "log_header_cog": "Cap",
    "log_header_sog": "Vitesse",
    "log_header_wind": "Vent",
    "log_header_precip": "Précip.",
    "log_header_cloud": "Nuages",
    "log_header_wave": "Vagues",
    "log_header_current": "Courant",
    "remarks_button_placeholder": "Remarque",
    "remarks_save_button": "Enregistrer",
    "remarks_cancel_button": "Annuler",
    "remarks_close_button": "Fermer",
    "remarks_save_forbidden": "Votre compte n'est pas autorisé à enregistrer des remarques.",
    "remarks_save_failed": "Échec de l'enregistrement, veuillez réessayer.",
    "remarks_unavailable": "Les remarques n'ont pas pu être chargées.",
}

DE: Dict[str, str] = {
    "logbook_title_suffix": "Bordbuch",
    "last_updated": "Zuletzt aktualisiert",
    "last_position": "Letzte Position",
    "vessel_call_sign": "Rufzeichen",
    "noscript_warning": (
        'Die Schaltflächen "Karte", "Details" und "Bemerkung" in diesem Bordbuch benötigen '
        "JavaScript zum Öffnen. Die meisten E-Mail-Programme entfernen das aus Anhängen -- öffnen "
        "Sie diese Datei in dem Fall in einem echten Browser (Chrome, Edge, Firefox, Safari, ...), "
        "statt sie direkt aus der E-Mail zu öffnen."
    ),
    "header_seq_abbr": "Nr.",
    "header_seq_full": "Laufende Nr.",
    "header_date": "Datum",
    "header_departure_abbr": "Abf.",
    "header_departure_full": "Abfahrt",
    "header_from": "Von",
    "header_arrival_abbr": "Ank.",
    "header_arrival_full": "Ankunft",
    "header_to": "Nach",
    "header_duration": "Dauer",
    "header_distance": "Distanz",
    "header_avg_speed": "Durchschn. Geschw.",
    "header_max_speed": "Max. Geschw.",
    "header_fuel": "Kraftstoff",
    "header_l_per_nm": "L/sm",
    "header_engine_hours": "Motorstunden",
    "header_rpm": "Drehzahl",
    "header_water_temp": "Wassertemperatur",
    "header_motion": "Bewegung",
    "header_route": "Route",
    "header_details": "Log",
    "header_remarks": "Bemerkungen",
    "week_label_prefix": "Woche",
    "totals_trips": "Fahrten",
    "totals_distance": "Gesamtdistanz",
    "totals_hours": "Gesamte Fahrstunden",
    "totals_fuel_calculated": "Gesamter Kraftstoff",
    "totals_fuel_engine_meter": "Gesamter Kraftstoff (Motorzähler)",
    "totals_avg_consumption": "Durchschn. Verbrauch",
    "totals_avg_speed": "Durchschn. Geschw.",
    "totals_top_speed": "Höchstgeschwindigkeit",
    "totals_engine_hour_meter": "Betriebsstundenzähler",
    "totals_engine_hour_meter_engine": "Betriebsstundenzähler, Motor {instance}",
    "totals_hours_logged": "Erfasste Motorstunden",
    "totals_hours_logged_engine": "Erfasste Motorstunden, Motor {instance}",
    "motion_roll": "Rollen",
    "motion_pitch": "Stampfen",
    "motion_peak": "Spitze",
    "rpm_tooltip_single": "durchschn. {avg} kn bei dieser Drehzahl ({min}-{max} kn)",
    "rpm_tooltip_single_fuel": "durchschn. {avg} kn bei dieser Drehzahl ({min}-{max} kn), durchschn. Verbrauch {fuel} L/sm",
    "rpm_tooltip_per_engine": "Motor {instance}: durchschn. {avg} kn ({min}-{max} kn)",
    "rpm_tooltip_per_engine_fuel": "Motor {instance}: durchschn. {avg} kn ({min}-{max} kn), durchschn. Verbrauch {fuel} L/sm",
    "max_speed_tooltip_single": "um {time} bei {rpm} U/min",
    "max_speed_tooltip_per_engine": "um {time}, Motor {instance}: {rpm} U/min",
    "map_button_show": "Karte",
    "map_marker_departure": "Abfahrt",
    "map_marker_arrival": "Ankunft",
    "map_marker_max_speed": "Höchstgeschwindigkeit",
    "details_button": "Log",
    "log_close_button": "Schließen",
    "log_header_number": "Nr.",
    "log_header_time": "Zeit",
    "log_header_position": "Position",
    "log_header_cog": "Kurs",
    "log_header_sog": "Geschwindigkeit",
    "log_header_wind": "Wind",
    "log_header_precip": "Niederschlag",
    "log_header_cloud": "Bewölkung",
    "log_header_wave": "Wellen",
    "log_header_current": "Strömung",
    "remarks_button_placeholder": "Bemerkung",
    "remarks_save_button": "Speichern",
    "remarks_cancel_button": "Abbrechen",
    "remarks_close_button": "Schließen",
    "remarks_save_forbidden": "Dein Konto darf keine Bemerkungen speichern.",
    "remarks_save_failed": "Speichern ist fehlgeschlagen, bitte versuche es erneut.",
    "remarks_unavailable": "Bemerkungen konnten nicht geladen werden.",
}

# strftime's %b is locale-independent (always English month abbreviations) unless the process
# locale is changed, which is fragile/platform-dependent -- a lookup table avoids that. Keyed by
# %b's own (always-English) output, same for every language table below.
MONTH_ABBR_NL: Dict[str, str] = {
    "Jan": "jan", "Feb": "feb", "Mar": "mrt", "Apr": "apr", "May": "mei", "Jun": "jun",
    "Jul": "jul", "Aug": "aug", "Sep": "sep", "Oct": "okt", "Nov": "nov", "Dec": "dec",
}

MONTH_ABBR_EN: Dict[str, str] = {
    "Jan": "Jan", "Feb": "Feb", "Mar": "Mar", "Apr": "Apr", "May": "May", "Jun": "Jun",
    "Jul": "Jul", "Aug": "Aug", "Sep": "Sep", "Oct": "Oct", "Nov": "Nov", "Dec": "Dec",
}

MONTH_ABBR_FR: Dict[str, str] = {
    "Jan": "janv", "Feb": "févr", "Mar": "mars", "Apr": "avr", "May": "mai", "Jun": "juin",
    "Jul": "juil", "Aug": "août", "Sep": "sept", "Oct": "oct", "Nov": "nov", "Dec": "déc",
}

MONTH_ABBR_DE: Dict[str, str] = {
    "Jan": "Jan", "Feb": "Feb", "Mar": "Mar", "Apr": "Apr", "May": "Mai", "Jun": "Jun",
    "Jul": "Jul", "Aug": "Aug", "Sep": "Sep", "Oct": "Okt", "Nov": "Nov", "Dec": "Dez",
}

# The generated page still renders server-side in Dutch by default (T = NL in html_writer.py) --
# these registries are only for the client-side language switcher (see html_writer.py's <script>).
LANGUAGES: Dict[str, Dict[str, str]] = {"nl": NL, "en": EN, "fr": FR, "de": DE}
MONTH_ABBR: Dict[str, Dict[str, str]] = {
    "nl": MONTH_ABBR_NL, "en": MONTH_ABBR_EN, "fr": MONTH_ABBR_FR, "de": MONTH_ABBR_DE,
}
# Plain text language codes for all four, not flag emoji -- found in practice: a regional-
# indicator flag emoji (see the "en" one specifically -- English isn't tied to one country the
# way Dutch/French/German are, so it stood out even as an emoji) renders as literal, unpaired
# letters (not a flag glyph at all) on at least one real device/browser this site is viewed from.
# Switching every language to plain text instead of just "en" keeps the four switcher buttons
# visually consistent with each other (see html_writer.py's .lang-flag-text styling), rather than
# reintroducing the asymmetry of one text label sitting next to three real flag emoji.
LANGUAGE_FLAGS: Dict[str, str] = {"nl": "NL", "en": "EN", "fr": "FR", "de": "DE"}
