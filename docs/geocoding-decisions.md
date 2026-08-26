# Havennaam-beslissingen (`geocode.py`)

Dit document legt vast **welke keuzes** er gemaakt zijn in hoe `geocode.py` een GPS-positie omzet
in een havennaam, en vooral **waarom** — inclusief wat er geprobeerd is en niet werkte. Bedoeld om
een volgende wijziging niet opnieuw dezelfde doodlopende paden te laten bewandelen.

Alle claims hieronder zijn geverifieerd tegen echte Nominatim/Overpass-data voor de 15 echte
reis-stops uit een echt logboek (zie "Referentieset" onderaan) — niet tegen aannames.

## Huidige regels (stand van commit `6aa7f02`)

1. **Eilandje via Overpass wint altijd**, ongeacht afstand. Alleen een losse naam-**node** telt
   mee; een kustlijn-**way** wordt genegeerd, ook niet als fallback.
2. **Marina/leisure-match van Nominatim zelf wint**, tenzij die match een losse punt-**node** is
   (geen vlak/way) én er ook een echt dorp/stad in het adres staat — dan wint het dorp.
3. Voor de rest: Nominatim's eigen `_PREFERRED_ADDRESS_KEYS`-volgorde (leisure/marina/harbour vóór
   town/village/city/...), ongewijzigd sinds het begin van het project.
4. Een match verder dan 250 m weg krijgt een prefix ("aan de kant, bij" / "op het water, bij")
   in plaats van te doen alsof de boot er precies is.

## Beslissing 1: eilandje — node wint altijd, way wordt genegeerd

**Wat**: `_nearby_islet_name` vraagt Overpass nu alleen nog naar `node["place"="islet"]`. De
`way`-variant (kustlijn) wordt niet meer opgevraagd en, waar ooit wel opgevraagd, nooit gebruikt —
ook niet als er geen node gevonden wordt.

**Waarom**:
- Een eilandje wordt in OSM meestal dubbel gemapt: een simpele naam-node (bv. "Île de la Jument")
  én een aparte kustlijn-way die een uitgebreidere naam kan dragen (bv. "Île de la Jument (Er
  Gazeg)"). De node-naam is korter en herkenbaarder voor een boot die er gewoon voor anker ligt,
  niet letterlijk in die ene inham — expliciet zo gewenst ("altijd node gebruiken").
- Een **way** se gerapporteerde afstand is onbetrouwbaar: Overpass' `out center` geeft het
  geometrische **centroïde** van de hele vorm terug, niet het dichtstbijzijnde punt van de
  kustlijn. Bij een groot/langgerekt eiland kan dat centroïde ver van de boot liggen, terwijl een
  randje van de kustlijn wél binnen de zoekstraal (300 m) valt — waardoor Overpass de way toch
  teruggeeft. **Echt geval**: bij Loctudy lag de boot 38 m van de haven-pier (overduidelijk
  daar), maar Île Garo's way-centroïde werd op ~680 m berekend en zou de haven onterecht hebben
  overstemd als de way als fallback was gebruikt.
- Een node heeft één exact punt, dus dat probleem speelt daar niet.

**Wat niet werkte / verworpen**:
- *Way als fallback met een afstandsgrens*: leek een optie, maar Overpass' eigen `around:300`-
  filter garandeert al dat de way-geometrie zelf binnen 300 m ligt — het probleem zit in het
  centroïde-punt, niet in de werkelijke afstand. Een afstandsgrens op het centroïde had Île Garo
  dus niet betrouwbaar tegengehouden.
- *Eilandje wint alleen als het dichterbij is dan het dorp*: expliciet afgewezen bij Kerners —
  "eilandje wint alleen als dichterbij kan toch niet in dit geval" / "altijd node gebruiken". Het
  hele punt van voor anker liggen bij een eilandje is vaak juist het eilandje zelf, ook als het
  dorp toevallig net iets dichterbij ligt.

## Beslissing 2: marina-node vs. dorp

**Wat**: `_pick_place_name` gebruikt Nominatim's eigen `leisure`-match (marina, meestal) zoals
altijd — **behalve** wanneer die match een losse punt-node is (`osm_type == "node"`, geen vlak)
én het adres ook een echt dorp/stad/gemeente bevat. Dan wint het dorp.

**Waarom**:
- **Echt geval**: op 47.4889,-3.1012 (Quiberon) matcht Nominatim een OSM-node genaamd "Darse de
  Castéro" (een specifiek, klein benoemd hoekje) in plaats van het bekende, herkenbare "Port
  Haliguen" — dat wél in hetzelfde adres stond, maar nooit werd gebruikt omdat de `leisure`-
  shortcut in `_pick_place_name` altijd als eerste checkt.
- **Onderscheidend kenmerk gevonden**: hoe de marina zelf in OSM gemapt is.
  - Port Olona, Port de Plaisance de Pornichet, Port du Crouesty, Concarneau: allemaal
    `osm_type: way` — een echt getekend havenbekken (bounding box van honderden meters).
  - Darse de Castéro: `osm_type: node` — een los puntje (bounding box ~11×11 m, geen echt vlak).
  - Dit is een **stabiele, tag-gebaseerde** eigenschap (hoe het element gemapt is), niet iets dat
    per Nominatim-aanroep kan wisselen.

**Wat niet werkte / verworpen** (elk getest tegen alle 15 echte posities voordat verworpen):

| Poging | Resultaat | Waarom verworpen |
|---|---|---|
| `_PREFERRED_ADDRESS_KEYS` herordenen (village vóór leisure/marina/harbour, altijd) | Fixt Darse de Castéro, maar verandert ook Port Olona → "aan de kant, bij Les Sables-d'Olonne", Pornichet → "Pornichet" (verliest de marinanaam) | Expliciet afgewezen: "aan de kant bij Port Olona is prima, niet wijzigen svp" |
| Algemene regel: skip `leisure`-shortcut zodra er een dorp/stad in het adres staat | Zelfde probleem als hierboven, plus Crouesty gaf bij twee losse live-aanroepen twee verschillende antwoorden ("Kerners" vs. "Port Navalo") — Nominatim's publieke dienst bleek hier zelf inconsistent tussen aanroepen | Onvoorspelbaar, naast dat het ook Port Olona/Pornichet raakt |
| `marina`/`harbour`-sleutel prefereren boven `leisure` (binnen `_PREFERRED_ADDRESS_KEYS`) | Geen enkel effect | Voor alle 15 posities komt een marina-match altijd alleen via het `leisure`-veld terug; `marina`/`harbour` komen in de praktijk nooit apart voor in deze dataset |
| Losse Overpass-zoekopdracht naar nabije marina's (met `has_village`-uitzondering, ooit gebouwd in commit `01f5758`) | Werkte, maar dupliceerde grotendeels wat Nominatim's eigen adres al deed | Volledig verwijderd deze sessie: een marina die écht ontbreekt in Nominatim's adres is een OSM-datahiaat, op te lossen via een OSM-edit, niet via extra code (zie Piriac-precedent) |
| Eén hardcoded uitzondering voor precies deze coördinaat | Zou werken, raakt niets anders | Verworpen ten gunste van de node/way-regel: die is generiek en dekt een hele klasse problemen, niet alleen dit ene punt |

## Waarom niet gewoon via OpenStreetMap oplossen?

Voor een **ontbrekende** marina (zoals het historische Piriac-geval) is de afspraak: dat lossen we
via een OSM-edit op, niet via code — zie de policy-beslissing hierboven bij de Overpass-marina-
zoekopdracht.

Voor Darse de Castéro lag het anders: de data zelf is niet fout (het is een legitieme, correct
getagde marina-node), het is alleen **minder relevant** dan het dorp ernaast voor iemand die het
logboek leest. Dat is geen datafout om in OSM te repareren, maar een presentatiekeuze — vandaar de
code-oplossing.

## Referentieset: de 15 echte reis-stops

Gebruikt om elke voorgestelde regel tegen te toetsen voordat die gebouwd werd. Bij een toekomstige
wijziging: eerst deze tabel opnieuw genereren en vergelijken voordat iets aangepast wordt.

| Coördinaat | Resultaat (stand van `6aa7f02`) |
|---|---|
| 46.4968,-1.7899 | Les Sables-d'Olonne |
| 46.5005,-1.7950 | aan de kant, bij Port Olona |
| 47.1080,-2.1157 | Pornic |
| 47.2579,-2.3507 | Port de Plaisance de Pornichet |
| 47.2749,-2.4246 | La Baule-Escoublac |
| 47.3445,-2.5135 | La Turballe |
| 47.3828,-2.5447 | Piriac-sur-Mer |
| 47.4889,-3.1012 | Port Haliguen |
| 47.5446,-2.8942 | Port du Crouesty |
| 47.5705,-2.8852 | Île de la Jument |
| 47.7108,-3.3550 | Port-Louis |
| 47.7108,-3.3551 | Port-Louis |
| 47.8387,-4.1759 | Loctudy |
| 47.8704,-3.9147 | Concarneau |
| 47.8776,-4.1213 | Sainte-Marine |

**Praktische tip voor een volgende sessie**: Overpass rate-limit't zwaar bij herhaald testen
(vaak `429`/`504`). Snel itereren gaat het best door eenmalig de ruwe Nominatim- én
Overpass-antwoorden voor deze 15 punten lokaal weg te schrijven (JSON, per coördinaat de twee
volledige payloads), en daarna elke variant van de logica direct tegen dat lokale bestand te
simuleren — geen verdere netwerkoproepen nodig totdat er een echt nieuwe positie bij komt.

## Nog open (bewust uitgesteld)

- **Sluizen/bruggen herkennen** ("De sluis bij ...", "De Ketelbrug") — nog geen echte data van
  beschikbaar; voorbeeld verwacht bij Barrage d'Arzal. Wachten op echte coördinaten voordat dit
  gebouwd wordt.
- Of het "eiland altijd node, way genegeerd"-principe ook voor andere featuretypes dan eilandjes
  zou moeten gelden, is nog niet onderzocht — nu bewust beperkt gehouden tot eilandjes, waar het
  concrete probleem zich voordeed.
