# nmea2000processor

Pure Python-applicatie die NMEA2000-logbestanden van een **Actisense W2K-2** omzet naar een
vaarlogboek: vertrek-/aankomsthaven, brandstofverbruik (uit motordata, niet uit een tanksensor)
en gedraaide motoruren. Schrijft vier bestanden weg (zelfde naam als `-o`, verschillende
extensie): een **CSV** (`.csv`), een **GPX** met de gevaren route per reis (`.gpx`, te openen in
navigatiesoftware zoals OpenCPN/Navionics), een **open werkmap** (`.ods`, opent in Excel en
LibreOffice/OpenOffice Calc) met dezelfde gegevens plus een totalenblad en een klikbaar
kaartplaatje per reis, en een **HTML-bestand** (`_routes.html`) met de volledige interactieve
kaart waar dat plaatje naartoe linkt.

Geen enkele runtime-dependency buiten de Python-standaardbibliotheek — alleen `pytest` als
dev-dependency voor de tests.

## Hoe het werkt

1. **Inlezen** — twee bestandsformaten, automatisch gekozen op basis van de extensie:
   - `.ebl` (`ebl_reader.py`): het binaire formaat van de **SD-kaart-logfunctie** van de W2K-2
     (BST-95 CAN-raw). Hier moet de app zelf NMEA2000 Fast-Packet-frames herassembleren. Dit
     formaat is reverse-engineered (zie "Aannames & beperkingen"), maar inmiddels wél
     gevalideerd tegen echte SD-kaartlogs van een W2K-2 met een Yanmar 4LV195Z-motor: een
     complete koude motorstart (brandstofdebiet, oliedruk-opbouw, opwarming, draaiurenteller,
     zelfs de "Preheat Indicator"-waarschuwing tijdens het voorgloeien) kwam er fysiek
     plausibel en intern consistent uit.
   - overig, bv. `.raw`/`.n2k` (`ascii_reader.py`): een *N2K ASCII*-logbestand, zoals je dat met
     `--live --tee` kunt vastleggen. Elke regel is al door de Actisense-hardware herassembleerd
     (fast-packet/multi-packet), dus daar is geen reassemblage nodig.
2. **Decoderen** (`pgn_decode.py`): pikt zes PGN's uit de stroom:
   - **127489** (*Engine Parameters, Dynamic*) → brandstofdebiet, draaiurenteller, en
     gezondheidsindicatoren (olie-druk/-temperatuur, koelvloeistoftemperatuur, alternator-
     spanning, motorbelasting) plus de twee "Discrete Status"-waarschuwingsvelden. Dit is
     motordata, dus expliciet niet de tankinhoud-sensor.
   - **127497** (*Trip Parameters, Engine*) → optioneel: de triptmeter-brandstofstand die de
     motor/ECU zelf bijhoudt (in liter), als het apparaat deze PGN verstuurt.
   - **128267** (*Water Depth*) → waterdiepte onder de transducer.
   - **129025** (*Position, Rapid Update*) → GPS-positie.
   - **129026** (*COG & SOG, Rapid Update*) → vaart over de grond (SOG, GPS-afgeleid). Dit is
     nadrukkelijk geen "speed through water" (dat zou PGN 128259 zijn, een paddlewheel-/
     logsensor — niet gebruikt door deze app en op de tot nu toe geteste boot ook niet aanwezig
     op de bus).
   - **126992** (*System Time*) → alleen gebruikt bij `.ebl`-bestanden, om frames van een
     absolute datum/tijd te voorzien (zie hieronder).
3. **Reizen herkennen** (`tripbuilder.py`): periodes waarin de boot lang genoeg stilligt
   (standaard ≥ 10 minuten, instelbaar via `--min-stop-minutes`) gelden als havenbezoek; de
   periodes daartussen zijn de reizen. Een groot gat in de data zelf (standaard ook 10 minuten,
   apart instelbaar via `--max-gap-minutes`) kapt een reis altijd af, ook als de stilligperiode
   vlak vóór het gat te kort was om als havenbezoek te tellen — anders zou een reis een gat
   overbruggen met een veel te lange gerapporteerde vaartijd (in de praktijk gevonden: 3:21
   i.p.v. de echte ~0:45, terwijl de draaiurenteller — die niet van GPS-classificatie afhangt —
   het wél bij het rechte eind had). Reizen korter dan 0.1 nm (instelbaar via
   `--min-trip-distance-nm`) worden weggefilterd: dat is vrijwel altijd GPS-/snelheidsruis
   vlak bij zo'n segmentgrens, geen echte reis. Per reis wordt berekend:
   - **Brandstofverbruik**, op twee manieren: **berekend** door het brandstofdebiet
     (PGN 127489) te integreren over de tijd, en — als beschikbaar — het verschil tussen
     begin- en eindstand van de **motor-eigen triptmeter** (PGN 127497). Let op: die
     triptmeter is een teller die de motor zelf beheert en kan door de gebruiker op het
     display gereset zijn, dus hij hoeft niet exact overeen te komen met onze eigen
     vertrek/aankomst-indeling.
   - **Draaiuren**: het verschil tussen de motoruren-teller bij vertrek en aankomst.
   - **Motorgezondheid**: gemiddelde olie-druk/-temperatuur, koelvloeistoftemperatuur,
     alternatorspanning en maximale motorbelasting tijdens de reis, plus een aparte
     **waarschuwingen**-kolom met alle actieve statusvlaggen (bv. "Low Oil Pressure") die
     ergens tijdens de reis voorkwamen.
   - **Snelheid**: gemiddelde en maximale vaart over de grond.
   - **Minimale waterdiepte**, inclusief de positie waar die werd gemeten.
4. **Havennamen** (`geocode.py`): de GPS-positie van elk havenbezoek wordt via
   OpenStreetMap/Nominatim (reverse geocoding) omgezet naar een plaatsnaam, met lokale
   caching zodat je nooit twee keer dezelfde positie opvraagt.
5. **Logboek wegschrijven** (`logbook_writer.py`): CSV met Engelse kolomnamen maar Nederlandse
   Excel-conventie voor de waarden (`;` als scheidingsteken, `,` als decimaalteken) — opent
   direct correct in de Nederlandse Excel.
6. **Route wegschrijven** (`gpx_writer.py`): naast de CSV wordt altijd ook een GPX-bestand
   geschreven (zelfde bestandsnaam, `.gpx`-extensie) met één track per reis. Klik je in een
   kaartprogramma op een track, dan zie je naam en beschrijving met vaartijd, afstand,
   brandstof en draaiuren van die reis.
7. **Open werkmap + kaarten** (`ods_writer.py`, `route_maps.py`, `static_map.py`): een
   `.ods`-bestand met twee bladen — 'Logbook' (dezelfde gegevens als de CSV, plus een
   'route'-kolom met een klein kaartplaatje + routelijn) en 'Totals' (opgeteld over alle
   reizen: aantal reizen, totale afstand, totale brandstof, totale draaiuren per motor). Het
   kaartplaatje is een losse OpenStreetMap-tegel die wordt opgehaald en lokaal gecachet
   (`.map_tile_cache/`); klik je erop, dan opent de volledige interactieve kaart
   (`_routes.html`, Leaflet + OpenStreetMap, kan in-/uitgezoomd worden). Vereist internet bij
   het aanmaken van het logboek — met `--no-route-thumbnails` sla je dat over (de kolom valt
   dan terug op een gewone tekstlink).

## Installatie

```bash
pip install -e ".[test]"
```

## Gebruik

De W2K-2 heeft drie onafhankelijke "data servers" (webinterface van het apparaat, standaard
poorten 60001-60003). Zet er één op **protocol TCP** en **formaat N2K ASCII** — die kun je dan
op twee manieren gebruiken:

### Optie A: opgeslagen logbestanden

**Van de SD-kaart** (geen live verbinding nodig — aanbevolen als je niet afhankelijk wilt zijn
van een verbinding tijdens het varen): download de `.ebl`-bestanden, óf handmatig via de
webinterface van de W2K-2 ("Download Logs"), óf automatisch met het meegeleverde
`nmea2log-download`-commando:

```bash
nmea2log-download
```

Dit leest instellingen (IP-adres/hostnaam, gebruikersnaam+wachtwoord óf een token, doelmap) uit
een configbestand, standaard `nmea2log.ini` **in de huidige map** (dus meestal de projectmap),
zodat je die niet telkens opnieuw hoeft in te typen. `nmea2log.ini` staat in `.gitignore` en
wordt dus nooit gecommit. Let op: deze projectmap staat wel in OneDrive, dus een wachtwoord hier
synct mee naar de cloud/je andere pc — een bewuste keuze; wil je dat niet, geef dan
`--config pad/buiten/onedrive/nmea2log.ini` mee. Het commando downloadt alleen wat nog ontbreekt
of onvolledig is (op bestandsgrootte vergeleken), dus opnieuw draaien na een volgende vaart haalt
alleen de nieuwe bestanden op. Standaard komen de bestanden in `Actisense/` (mapstructuur
`EBL000000/`, `EBL000001/`, ... eronder), ook in `.gitignore`.

Verwerk de gedownloade bestanden vervolgens zoals gewoonlijk:

```bash
nmea2log Actisense/EBL000000/*.ebl Actisense/EBL000001/*.ebl -o logbook.csv
```

Dit EBL-pad is reverse-engineered (zie "Aannames & beperkingen") en inmiddels gevalideerd tegen
echte SD-kaartlogs — controleer bij twijfel altijd of de uitkomst logisch aanvoelt voor jouw
eigen vaart/motor.

**Alternatief**: leg de N2K ASCII-stream van een Data Server vast naar een bestand, bijvoorbeeld
door `nmea2log --live ... --tee 2026-07-15.raw` te draaien (zie Optie B), of met een ander
terminalprogramma dat de TCP-stream naar een bestand wegschrijft. Noem het bestand bij voorkeur
met een datum erin, bijvoorbeeld `2026-07-15.raw` — dat wordt gebruikt om middernacht-
doorgangen correct te herkennen (het tijdstip in het formaat bevat zelf geen datum; `.ebl`-
bestanden hebben dit probleem niet, die halen hun tijd uit de data zelf).

```bash
nmea2log 2026-07-15.raw -o logbook.csv
```

Dit schrijft `logbook.csv`, `logbook.gpx` (de route per reis), `logbook.ods` (open werkmap met
totalenblad en kaartplaatjes) en `logbook_routes.html` (de interactieve kaart).

Meerdere bestanden (bijvoorbeeld één per dag, `.ebl` en `.raw` door elkaar) in één keer
verwerken:

```bash
nmea2log 2026-07-14.raw 2026-07-15.ebl 2026-07-16.raw -o logbook.csv
```

### Optie B: live meelezen

Verbind rechtstreeks met de W2K-2 terwijl je vaart. Vervang `192.168.4.1` door het IP-adres
van de W2K-2 op jouw netwerk (te vinden op de statuspagina/webinterface van het apparaat):

```bash
nmea2log --live 192.168.4.1 -o logbook.csv
```

De sessie loopt door tot je op Ctrl+C drukt (of tot `--duration` verstrijkt); daarna wordt het
logboek geschreven met alles wat tot dan toe is binnengekomen — een reis die nog niet is
afgesloten met een nieuw havenbezoek krijgt "Onbekend (einde buiten logbestand)" als
aankomsthaven. Met `--tee` bewaar je tegelijk de ruwe ASCII-stream naar een bestand, zodat je
zowel live verwerkt als een permanent logbestand overhoudt:

```bash
nmea2log --live 192.168.4.1:60001 --tee 2026-07-16.raw -o logbook.csv
```

### Nuttige opties

| Optie | Betekenis |
|---|---|
| `--live HOST[:PORT]` | Live verbinden met de W2K-2 over TCP i.p.v. bestanden verwerken (standaardpoort 60001) |
| `--tee PAD` | Alleen bij `--live`: bewaar de ruwe inkomende ASCII-regels ook naar dit bestand |
| `--duration SECONDEN` | Alleen bij `--live`: stop automatisch na dit aantal seconden |
| `--speed-threshold-kn` | Vaart (kn) waaronder de boot als 'stilliggend' geldt (standaard 0.5) |
| `--min-stop-minutes` | Minimale stilligduur om als havenbezoek te tellen (standaard 10) |
| `--max-gap-minutes` | Vanaf hoeveel minuten zonder data een reis wordt afgekapt (standaard: zelfde als `--min-stop-minutes`) |
| `--min-trip-distance-nm` | Reizen korter dan dit worden weggefilterd als ruis i.p.v. getoond (standaard 0.1 nm) |
| `--no-geocode` | Geen internet nodig; toont coördinaten in plaats van havennamen |
| `--cache-file` | Pad naar het cachebestand voor havennamen (standaard `.geocode_cache.json`) |
| `--start-date` | Forceer de startdatum van het eerste logbestand (`YYYY-MM-DD`); niet van toepassing bij `--live` |
| `--utc-offset UREN` | Vaste tijdzone-offset (bv. `2` voor CEST) voor de weergegeven tijden. Standaard: automatisch geschat per reis uit de vertreklengtegraad |
| `--no-route-thumbnails` | Sla het ophalen van kaartplaatjes voor de ODS 'route'-kolom over (geen internet nodig; kolom valt terug op een tekstlink) |

Alle NMEA2000-tijden zijn UTC; in de CSV en de GPX-tracknamen wordt dit omgerekend naar lokale
tijd. Zonder `--utc-offset` wordt de offset per reis geschat uit de lengtegraad van het
vertrekpunt (15° per uur) — een grove schatting zonder tijdzone-database (dus geen
zomer-/wintertijd-besef, en kan vlak bij een tijdzone-grens tot ~1 uur afwijken), maar wel zonder
extra dependency. `<trkpt><time>` in de GPX blijft altijd strikt UTC, conform de GPX-conventie.
De "vaartijd"-kolom is offset-onafhankelijk (het is een duur, geen tijdstip).

### Configbestand voor standaardwaarden

In plaats van bovenstaande opties elke keer op de command line mee te geven, kun je ze in de
`[nmea2log]`-sectie van `nmea2log.ini` zetten (zie ook "Downloaden via de W2K-2 web-API" hierboven
voor de `[w2k2]`-sectie in hetzelfde bestand). Command-line-argumenten overschrijven altijd wat in
het configbestand staat. Voorbeeld:

```ini
[nmea2log]
min_trip_distance_nm = 0.3
utc_offset = 2
no_geocode = false
```

## Tests

```bash
pytest
```

## Aannames & beperkingen

- **`nmea2log-download`-API**: net als het EBL-bestandsformaat zelf is de web-API van de W2K-2
  (`/api/data_logs`, `/api/download`, login/token) nooit officieel door Actisense gepubliceerd —
  geobserveerd via browser-DevTools op de firmware-webapp en kan wijzigen bij firmware-updates.
  Met name de veldnaam waarin het login-token terugkomt is niet 100% bevestigd (`w2k2_download.py`
  probeert een aantal gangbare namen, zie `_TOKEN_KEYS`); mocht inloggen lukken maar geen token
  gevonden worden, dan toont het commando de ruwe response zodat de juiste naam toegevoegd kan
  worden.
- **Regelformaat (N2K ASCII)**: de parser is gebouwd op basis van de officiële Actisense-
  documentatie op de website — het kennisbank-artikel
  ["NMEA 2000 ASCII Output format"](https://actisense.com/knowledge-base/nmea-2000/w2k-1-nmea-2000-to-wifi-gateway/nmea-2000-ascii-output-format/)
  en de [W2K-2 User Manual](https://actisense.com/products/w2k-2-nmea-2000-wifi-gateway/) (zie
  productpagina, downloads-tab) — en het [canboat](https://github.com/canboat/canboat)-PGN-
  woordenboek. Ik heb dit niet tegen een echte log van jouw W2K-2 kunnen testen — controleer de
  eerste paar regels van een echt logbestand tegen de regex in `ascii_reader.py` (`_LINE_RE`) en
  pas die aan als het afwijkt.
- **EBL-formaat (SD-kaartlog)**: dit formaat is door Actisense nooit officieel gepubliceerd.
  `ebl_reader.py` is gebaseerd op reverse-engineering door de open-source Go-bibliotheek
  [aldas/go-nmea-client](https://github.com/aldas/go-nmea-client) (specifiek
  [`actisense/eblreader.go`](https://github.com/aldas/go-nmea-client/blob/main/actisense/eblreader.go) —
  framing, byte-stuffing, CAN-ID-decodering) — met de hand geverifieerd tegen de testvectoren
  daarin, én inmiddels gevalideerd tegen ~800 MB echte SD-kaartlogs van een W2K-2 (Yanmar
  4LV195Z-sterndrive): een complete koude motorstart kwam er fysiek plausibel en intern
  consistent uit (brandstofdebiet, oliedruk-opbouw, spanningsverval tijdens het starten,
  opwarming, draaiurenteller die precies bijhield, en zelfs een "Preheat Indicator"-waarschuwing
  exact tijdens het voorgloeien). Bekende beperkingen/aannames:
  - De eigen 2-byte tijdteller per record wordt genegeerd (de betekenis ervan is nergens
    betrouwbaar gedocumenteerd — zelfs de referentie-implementatie gokt ernaar). In plaats
    daarvan wordt de absolute tijd afgeleid uit PGN 126992 (System Time) elders in de stream.
    **Gevolg**: als je NMEA2000-netwerk geen bron heeft die PGN 126992 verstuurt (meestal een
    GPS/kaartplotter), levert een `.ebl`-bestand niets op — frames vóór de eerste 126992-
    boodschap worden overgeslagen, en zonder 126992 helemaal geen frames.
  - Fast-Packet-reassemblage (nodig voor PGN 127489 en 127497, die beide >8 bytes zijn) is
    geïmplementeerd volgens de standaard NMEA2000-conventie en inmiddels ook tegen echte
    fast-packet-data gevalideerd (zie hierboven).
  - Geef bij onverwachte uitkomsten een klein `.ebl`-fragment door, dan wordt dit samen tegen
    echte data gecontroleerd.
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
- **Meerdere bronnen voor dezelfde PGN**: sommige boten hebben meerdere apparaten die positie,
  vaart-over-de-grond of diepte versturen (bv. twee GPS-antennes). Dit is met echte data
  gemeten en bevestigd: op een boot met twee GPS-ontvangers gaven die op hetzelfde moment een
  paar meter positieverschil (mediaan 3,2 m, max 7,0 m over ~6000 vergelijkingen) en een
  fractie knoop snelheidsverschil (mediaan 0,2 kn, max 1,9 kn over drie bronnen). Op zichzelf
  klein, maar zonder filtering worden die onafhankelijke metingen puur op tijd door elkaar
  gesorteerd, wat voor duizenden valse kleine "sprongen" zorgt — in de praktijk viel de
  afstand van een reis daardoor in eerste instantie 10x te hoog uit (153,7 i.p.v. 13,5 nm).

  **Hoe de app dit oplost** (`_select_primary_gps_source` in `cli.py`): de bron met de meeste
  positieberichten (PGN 129025) geldt als **primaire GPS**, en de snelheid (PGN 129026) van
  **diezelfde fysieke bron** wordt gebruikt — bewust niet onafhankelijk de "beste" bron per PGN
  gekozen, want dan zouden positie en snelheid uit twee verschillende apparaten kunnen komen en
  een moeilijk te doorgronden inconsistentie tussen track en stilliggend/varend-classificatie
  ontstaan. Alleen als de gekozen positiebron zelf geen snelheid stuurt, valt de code terug op
  de snelheidsbron met de meeste berichten (dan dus wél een ander apparaat). Diepte wordt
  onafhankelijk gekozen (geen GPS-gerelateerde PGN, dus geen reden om aan dezelfde bron te
  koppelen). De CLI meldt op stderr welke bron als primair gekozen is zodra er meerdere zijn.
  **Kanttekening**: "meeste berichten" is een proxy, geen kwaliteitsbeoordeling — er wordt niet
  gekeken naar GPS-nauwkeurigheid (HDOP, aantal satellieten, fix-type).
- **Datum**: het N2K ASCII-formaat bevat alleen een tijdstip, geen datum. Zorg dat elk
  logbestand een datum in de naam heeft (`YYYY-MM-DD...`), anders wordt de
  bestandswijzigingsdatum gebruikt.
- **Waterdiepte**: de app gebruikt de rauwe "Depth"-waarde uit PGN 128267 (diepte onder de
  transducer), zonder de transducer-offset erbij op te tellen — meestal is dat al de waarde
  die instrumenten standaard tonen, maar controleer dit tegen je eigen dieptemeter-instelling.
- **Motorwaarschuwingen**: de bitbetekenissen (bv. "Low Oil Pressure") komen uit de generieke
  NMEA2000-standaardlijst (canboat's ENGINE_STATUS_1/2). Sommige fabrikanten gebruiken hiervan
  afwijkende of extra proprietary statusbits — controleer dit tegen je eigen motor-documentatie
  als een waarschuwing onverwacht verschijnt of ontbreekt.
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
