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

De W2K-2 heeft drie onafhankelijke "data servers" (webinterface van het apparaat, standaard
poorten 60001-60003). Zet er één op **protocol TCP** en **formaat N2K ASCII** — die kun je dan
op twee manieren gebruiken:

### Optie A: opgeslagen logbestanden

Leg de stream vooraf vast naar een bestand (bijvoorbeeld met de SD-kaart-logfunctie van de
W2K-2, of door met een terminalprogramma de TCP-stream naar een bestand te loggen). Noem het
bestand bij voorkeur met een datum erin, bijvoorbeeld `2026-07-15.raw` — dat wordt gebruikt om
middernacht-doorgangen correct te herkennen (het tijdstip in het formaat bevat zelf geen datum).

```bash
nmea2000logboek 2026-07-15.raw -o logboek.csv
```

Meerdere bestanden (bijvoorbeeld één per dag) in één keer verwerken:

```bash
nmea2000logboek 2026-07-14.raw 2026-07-15.raw 2026-07-16.raw -o logboek.csv
```

### Optie B: live meelezen

Verbind rechtstreeks met de W2K-2 terwijl je vaart. Vervang `192.168.4.1` door het IP-adres
van de W2K-2 op jouw netwerk (te vinden op de statuspagina/webinterface van het apparaat):

```bash
nmea2000logboek --live 192.168.4.1 -o logboek.csv
```

De sessie loopt door tot je op Ctrl+C drukt (of tot `--duration` verstrijkt); daarna wordt het
logboek geschreven met alles wat tot dan toe is binnengekomen — een reis die nog niet is
afgesloten met een nieuw havenbezoek krijgt "Onbekend (einde buiten logbestand)" als
aankomsthaven. Met `--tee` bewaar je tegelijk de ruwe ASCII-stream naar een bestand, zodat je
zowel live verwerkt als een permanent logbestand overhoudt:

```bash
nmea2000logboek --live 192.168.4.1:60001 --tee 2026-07-16.raw -o logboek.csv
```

### Nuttige opties

| Optie | Betekenis |
|---|---|
| `--live HOST[:PORT]` | Live verbinden met de W2K-2 over TCP i.p.v. bestanden verwerken (standaardpoort 60001) |
| `--tee PAD` | Alleen bij `--live`: bewaar de ruwe inkomende ASCII-regels ook naar dit bestand |
| `--duration SECONDEN` | Alleen bij `--live`: stop automatisch na dit aantal seconden |
| `--speed-threshold-kn` | Vaart (kn) waaronder de boot als 'stilliggend' geldt (standaard 0.5) |
| `--min-stop-minutes` | Minimale stilligduur om als havenbezoek te tellen (standaard 10) |
| `--no-geocode` | Geen internet nodig; toont coördinaten in plaats van havennamen |
| `--cache-file` | Pad naar het cachebestand voor havennamen (standaard `.geocode_cache.json`) |
| `--start-date` | Forceer de startdatum van het eerste logbestand (`YYYY-MM-DD`); niet van toepassing bij `--live` |

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
- **Live-modus (`--live`)** ondersteunt alleen **TCP** (de W2K-2-handleiding raadt dit ook aan
  vanwege ingebouwde foutcorrectie; UDP-only is niet geïmplementeerd). Bij een verbroken
  verbinding stopt de sessie en wordt het logboek geschreven met wat er tot dan toe is
  binnengekomen — er wordt niet automatisch opnieuw verbonden. De standaardpoort (60001) komt
  overeen met "Data Server 1" op de W2K-2; controleer in de webinterface van het apparaat welke
  server op TCP + N2K ASCII staat en welke poort die gebruikt.

## Samenwerken met Claude Code aan dit project

- Werk in kleine, verifieerbare stappen: laat na elke wijziging `pytest` draaien voordat je
  verdergaat — de teststructuur hierboven (`tests/`) is er juist op gericht dat snel te kunnen.
- Geef bij nieuwe features concrete voorbeelddata mee (een stukje echte of realistische
  logregel), zeker voor alles wat met PGN-decodering te maken heeft — dat scheelt giswerk.
- Zodra je een echt logbestand van de W2K-2 hebt: deel een klein fragment (een paar honderd
  regels volstaat) zodat het parseerformaat en de PGN-aannames tegen echte data geverifieerd
  kunnen worden.
