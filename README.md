# nmea2000processor

Pure Python-applicatie die NMEA2000-logbestanden van een **Actisense W2K-2** omzet naar een
vaarlogboek (CSV): vertrek-/aankomsthaven, brandstofverbruik (uit motordata, niet uit een
tanksensor) en gedraaide motoruren.

Geen enkele runtime-dependency buiten de Python-standaardbibliotheek — alleen `pytest` als
dev-dependency voor de tests.

## Hoe het werkt

1. **Inlezen** (`ascii_reader.py`): leest een *N2K ASCII*-logbestand zoals de W2K-2 dat
   wegschrijft. Elke regel is al door de Actisense-hardware herassembleerd (fast-packet/
   multi-packet), dus er is geen CAN-framereassemblage nodig.
2. **Decoderen** (`pgn_decode.py`): pikt drie PGN's uit de stroom:
   - **127489** (*Engine Parameters, Dynamic*) → brandstofdebiet (L/uur) en de cumulatieve
     draaiurenteller van de motor. Dit is motordata, dus expliciet niet de tankinhoud-sensor.
   - **129025** (*Position, Rapid Update*) → GPS-positie.
   - **129026** (*COG & SOG, Rapid Update*) → vaart over de grond.
3. **Reizen herkennen** (`tripbuilder.py`): periodes waarin de boot lang genoeg stilligt
   (standaard ≥ 10 minuten, instelbaar) gelden als havenbezoek; de periodes daartussen zijn
   de reizen. Brandstofverbruik per reis wordt berekend door het brandstofdebiet te
   integreren over de tijd; draaiuren per reis zijn het verschil tussen de motoruren-teller
   bij vertrek en aankomst.
4. **Havennamen** (`geocode.py`): de GPS-positie van elk havenbezoek wordt via
   OpenStreetMap/Nominatim (reverse geocoding) omgezet naar een plaatsnaam, met lokale
   caching zodat je nooit twee keer dezelfde positie opvraagt.
5. **Logboek wegschrijven** (`logbook_writer.py`): CSV met `;` als scheidingsteken en `,` als
   decimaalteken — opent direct correct in de Nederlandse Excel.

## Installatie

```bash
pip install -e ".[test]"
```

## Gebruik

Zet je W2K-2 (via de Actisense NDC-configuratietool) in **N2K ASCII**-uitvoermodus en leg de
stream vast naar een bestand (bijvoorbeeld via de logfunctie van NDC, of door de WiFi/TCP-
stream met een terminalprogramma naar een bestand te loggen). Noem het bestand bij voorkeur
met een datum erin, bijvoorbeeld `2026-07-15.raw` — dat wordt gebruikt om middernacht-
doorgangen correct te herkennen (het tijdstip in het formaat bevat zelf geen datum).

```bash
nmea2000logboek 2026-07-15.raw -o logboek.csv
```

Meerdere bestanden (bijvoorbeeld één per dag) in één keer verwerken:

```bash
nmea2000logboek 2026-07-14.raw 2026-07-15.raw 2026-07-16.raw -o logboek.csv
```

### Nuttige opties

| Optie | Betekenis |
|---|---|
| `--speed-threshold-kn` | Vaart (kn) waaronder de boot als 'stilliggend' geldt (standaard 0.5) |
| `--min-stop-minutes` | Minimale stilligduur om als havenbezoek te tellen (standaard 10) |
| `--no-geocode` | Geen internet nodig; toont coördinaten in plaats van havennamen |
| `--cache-file` | Pad naar het cachebestand voor havennamen (standaard `.geocode_cache.json`) |
| `--start-date` | Forceer de startdatum van het eerste logbestand (`YYYY-MM-DD`) |

## Tests

```bash
pytest
```

## Aannames & beperkingen

- **Regelformaat**: de parser is gebouwd op basis van de officiële Actisense-documentatie
  ("NMEA 2000 ASCII Output format") en het canboat-PGN-woordenboek. Ik heb dit niet tegen een
  echte log van jouw W2K-2 kunnen testen — controleer de eerste paar regels van een echt
  logbestand tegen de regex in `ascii_reader.py` (`_LINE_RE`) en pas die aan als het afwijkt.
- **Havenherkenning** is gebaseerd op stilligtijd + reverse geocoding, niet op een lijst van
  bekende marina's. Nominatim geeft niet altijd de exacte marinanaam terug (soms de plaatsnaam
  van de dichtstbijzijnde bebouwing). Wil je preciezere namen, dan is de volgende stap een
  eigen havenlijst (naam + coördinaten + straal) toevoegen die eerst geraadpleegd wordt.
  Nominatim's gebruiksbeleid staat maximaal 1 verzoek/seconde toe; dat wordt gerespecteerd,
  maar bij zware/professionele inzet is een eigen Nominatim-instance of betaalde dienst beter.
  Geeft die verkeerde namen dan controleer de rauwe cache in `.geocode_cache.json`.
- **Meerdere motoren**: de code ondersteunt meerdere `instance`-nummers (brandstof wordt
  gesommeerd, draaiuren per motor apart getoond), maar is niet getest met een echte
  twin-engine-installatie.
- **Datum**: het N2K ASCII-formaat bevat alleen een tijdstip, geen datum. Zorg dat elk
  logbestand een datum in de naam heeft (`YYYY-MM-DD...`), anders wordt de
  bestandswijzigingsdatum gebruikt.

## Samenwerken met Claude Code aan dit project

- Werk in kleine, verifieerbare stappen: laat na elke wijziging `pytest` draaien voordat je
  verdergaat — de teststructuur hierboven (`tests/`) is er juist op gericht dat snel te kunnen.
- Geef bij nieuwe features concrete voorbeelddata mee (een stukje echte of realistische
  logregel), zeker voor alles wat met PGN-decodering te maken heeft — dat scheelt giswerk.
- Zodra je een echt logbestand van de W2K-2 hebt: deel een klein fragment (een paar honderd
  regels volstaat) zodat het parseerformaat en de PGN-aannames tegen echte data geverifieerd
  kunnen worden.
