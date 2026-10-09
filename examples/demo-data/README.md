# Demo .ebl files

Five `.ebl` files, one per trip, of a **made-up cruise**: what an Actisense W2K-2 would have logged on a fictional boat sailing
Enkhuizen - Medemblik - Den Oever - Oudeschild (Texel) - West-Terschelling - Enkhuizen on 14-18 June 2025. Everything in them is
computed by [`generate_demo_ebl.py`](../generate_demo_ebl.py): there is no real boat, person or recording behind them. They hold what
a boat with a small diesel engine, a wind instrument and a depth sounder puts on its NMEA 2000 network: position, speed and course,
engine revolutions, fuel rate and hours, oil and coolant, water and air temperature, humidity, wind, depth, heel and battery.

Importing them builds the five trips of the [demo logbook](https://ayuus.github.io/nmea2log/examples/demo-logbook.html), the same
numbers (the page and the files come from the same route, [`demo_cruise.py`](../demo_cruise.py)). The place names and the weather
are looked up on the internet while the logbook is built, so the names in your logbook can differ a little from the demo page.

```
Actisense/
  EBL000001/
    000001_001.ebl   Enkhuizen -> Medemblik
    000001_002.ebl   Medemblik -> Den Oever
    000001_003.ebl   Den Oever -> Oudeschild (Texel)
    000001_004.ebl   Oudeschild (Texel) -> West-Terschelling
    000001_005.ebl   West-Terschelling -> Enkhuizen
```

## Try it in the Android app

1. Download [`demo-ebl.zip`](https://github.com/Ayuus/nmea2log/releases/download/demo-data/demo-ebl.zip) on the phone and unzip it (in
   the Files app, open the zip and tap *Extract*): that makes a folder `demo-ebl` in the phone's **Download** folder (some apps call it "Downloads"), with the folder `Actisense` in it.
2. In the app, tap **Import** (the second button) and pick that folder. Android does not let an app pick the Download folder itself, only a
   folder inside it: pick `demo-ebl` (or `Actisense`), then *Use this folder* and *Allow*.
3. The app copies the five files and builds the logbook. Tap **View logbook**.

To start over with your own data afterwards, use Settings > Local .ebl files > Delete.

## Try it in the iOS app

1. Download [`demo-ebl.zip`](https://github.com/Ayuus/nmea2log/releases/download/demo-data/demo-ebl.zip) and unzip it (in the Files
   app, tap the zip).
2. In the app, tap **Import** (the second button) and pick the unzipped `Actisense` folder. Or copy that folder into the app's own
   folder in Files (On My iPhone > My Sailing Logbook) and tap **Assemble**.
3. Tap **View logbook**: five trips.

## Try it on a computer

```
python -m nmea2log --no-upload --ebl-dir examples/demo-data/Actisense -o logbook.csv
```

builds `logbook.html` (and a CSV) next to where you run it. (`--no-upload` matters if you also have a `nmea2log.ini` that uploads.)

## Making them again

`python examples/generate_demo_ebl.py` writes the same files (a fixed random seed), so they can be checked against these: the
tests do. After a change to the cruise (`demo_cruise.py`) also run `python examples/generate_demo_logbook.py` so the demo page
follows, and make `demo-ebl.zip` again for the release `demo-data`.
