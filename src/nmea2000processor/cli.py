from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, TypeVar

from .ascii_reader import iter_frames
from .ebl_reader import iter_frames as iter_frames_ebl
from .geocode import Geocoder, NoGeocoder
from .gpx_writer import write_gpx
from .logbook_writer import write_csv
from .model import DepthSample, EngineSample, Frame, PositionFix, SogSample, TripFuelSample
from .network_reader import DEFAULT_PORT, iter_frames_tcp
from .pgn_decode import (
    PGN_COG_SOG_RAPID,
    PGN_ENGINE_DYNAMIC,
    PGN_POSITION_RAPID,
    PGN_TRIP_FUEL_ENGINE,
    PGN_WATER_DEPTH,
    decode_engine_dynamic,
    decode_position_rapid,
    decode_sog,
    decode_trip_fuel_engine,
    decode_water_depth,
)
from .tripbuilder import build_trips

_T = TypeVar("_T")


def _dominant_source_only(by_source: Dict[int, List[_T]]) -> List[_T]:
    """Sommige boten hebben meerdere apparaten die dezelfde PGN sturen (bv. twee GPS-
    antennes die allebei positie of vaart over de grond versturen). Zonder filtering worden
    hun onafhankelijke, licht afwijkende metingen puur op tijd door elkaar gesorteerd, wat
    voor honderden valse kleine "sprongen" zorgt die samen de afstand flink kunnen opblazen.
    We houden daarom alleen de bron aan die de meeste berichten stuurde -- over de hele sessie
    (alle bestanden/de hele live-verbinding) samen, niet per bestand, anders kan een andere
    bron "winnen" in elk bestand en het probleem juist terugkomen op de naad tussen bestanden."""
    if not by_source:
        return []
    dominant_source = max(by_source, key=lambda source: len(by_source[source]))
    return by_source[dominant_source]


def _merge_by_source(target: Dict[int, List[_T]], addition: Dict[int, List[_T]]) -> None:
    for source, items in addition.items():
        target.setdefault(source, []).extend(items)


def _select_primary_gps_source(
    fixes_by_source: Dict[int, List[PositionFix]], sogs_by_source: Dict[int, List[SogSample]]
) -> Tuple[List[PositionFix], List[SogSample], Optional[int]]:
    """Kiest één samenhangende primaire GPS-bron voor positie én snelheid samen, in plaats van
    onafhankelijk per PGN te kiezen (zoals ``_dominant_source_only`` op zichzelf zou doen).

    Waarom: met echte data gemeten dat twee GPS-ontvangers op dezelfde boot een paar meter
    positieverschil en een fractie knoop snelheidsverschil geven -- op zichzelf klein, maar als
    de "winnende" positiebron en de "winnende" snelheidsbron toevallig twee verschillende
    fysieke apparaten zijn, ontstaat een moeilijk te doorgronden inconsistentie tussen track en
    snelheid-classificatie (stilliggend/varend). Door snelheid van dezelfde bron te pakken als
    de gekozen positiebron, blijft dat samenhangend.

    Aanpak: de bron met de meeste positieberichten (PGN 129025) is leidend. Snelheid van
    diezelfde bron wordt gebruikt; alleen als die bron zelf geen snelheid stuurde, valt de code
    terug op de snelheidsbron met de meeste berichten (dan dus wél een ander fysiek apparaat
    dan de positiebron -- beter dan alle bronnen door elkaar mengen, maar niet ideaal)."""
    if not fixes_by_source:
        return [], _dominant_source_only(sogs_by_source), None

    primary_source = max(fixes_by_source, key=lambda source: len(fixes_by_source[source]))
    fixes = fixes_by_source[primary_source]
    sogs = sogs_by_source.get(primary_source) or _dominant_source_only(sogs_by_source)
    return fixes, sogs, primary_source


def _collect_samples(
    frames: Iterable[Frame], *, deadline: Optional[float] = None
) -> Tuple[
    Dict[int, List[PositionFix]],
    Dict[int, List[SogSample]],
    List[EngineSample],
    List[TripFuelSample],
    Dict[int, List[DepthSample]],
]:
    """Verwerkt frames tot samples, gegroepeerd per bronadres voor PGN's die van meerdere
    apparaten tegelijk kunnen komen. Stopt netjes op Ctrl+C of als de deadline verstrijkt,
    zodat een live-sessie altijd een logboek oplevert van wat er tot dan toe binnen is."""
    fixes_by_source: Dict[int, List[PositionFix]] = {}
    sogs_by_source: Dict[int, List[SogSample]] = {}
    depth_by_source: Dict[int, List[DepthSample]] = {}
    engine_samples: List[EngineSample] = []
    trip_fuel_samples: List[TripFuelSample] = []
    try:
        for frame in frames:
            if deadline is not None and time.monotonic() >= deadline:
                print("Duur verstreken; live-sessie wordt afgesloten...", file=sys.stderr)
                break
            if frame.pgn == PGN_POSITION_RAPID:
                decoded = decode_position_rapid(frame.data)
                if decoded is not None:
                    lat, lon = decoded
                    fixes_by_source.setdefault(frame.source, []).append(PositionFix(frame.time, lat, lon))
            elif frame.pgn == PGN_COG_SOG_RAPID:
                sog = decode_sog(frame.data)
                if sog is not None:
                    sogs_by_source.setdefault(frame.source, []).append(SogSample(frame.time, sog))
            elif frame.pgn == PGN_ENGINE_DYNAMIC:
                decoded = decode_engine_dynamic(frame.data)
                if decoded is not None:
                    engine_samples.append(EngineSample(time=frame.time, **decoded))
            elif frame.pgn == PGN_TRIP_FUEL_ENGINE:
                decoded = decode_trip_fuel_engine(frame.data)
                if decoded is not None:
                    instance, trip_fuel_l = decoded
                    trip_fuel_samples.append(TripFuelSample(frame.time, instance, trip_fuel_l))
            elif frame.pgn == PGN_WATER_DEPTH:
                depth_m = decode_water_depth(frame.data)
                if depth_m is not None:
                    depth_by_source.setdefault(frame.source, []).append(DepthSample(frame.time, depth_m))
    except KeyboardInterrupt:
        print("\nOnderbroken door gebruiker; logboek wordt geschreven met de tot nu toe verzamelde data...", file=sys.stderr)
    return fixes_by_source, sogs_by_source, engine_samples, trip_fuel_samples, depth_by_source


def _iter_frames_for_path(path: Path, start_date: Optional[date]) -> Iterable[Frame]:
    """Kiest de juiste parser op basis van de bestandsextensie: .ebl -> binaire SD-kaartlog,
    al het overige -> N2K ASCII (live-TCP-stream vastgelegd naar bestand, zie --tee)."""
    if path.suffix.lower() == ".ebl":
        return iter_frames_ebl(path)
    return iter_frames(path, start_date=start_date)


def _parse_host_port(value: str, default_port: int) -> Tuple[str, int]:
    if ":" in value:
        host, _, port_str = value.rpartition(":")
        return host, int(port_str)
    return value, default_port


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nmea2log",
        description="Zet NMEA2000-data van een Actisense W2K-2 (N2K ASCII) om in een vaarlogboek (CSV), "
        "uit opgeslagen logbestanden of live via een TCP-verbinding.",
    )
    parser.add_argument(
        "logfiles",
        nargs="*",
        type=Path,
        help="Eén of meer logbestanden: .ebl (SD-kaartlog van de W2K-2) of .raw/.n2k (N2K ASCII, "
        "bv. vastgelegd via --live --tee). Niet combineren met --live.",
    )
    parser.add_argument(
        "--live",
        metavar="HOST[:PORT]",
        default=None,
        help=f"Verbind live met de W2K-2 over TCP (bv. 192.168.4.1 of 192.168.4.1:60001; "
        f"standaardpoort {DEFAULT_PORT}). Loopt tot Ctrl+C of --duration verstrijkt.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Alleen bij --live: stop automatisch na dit aantal seconden",
    )
    parser.add_argument(
        "--tee",
        type=Path,
        default=None,
        help="Alleen bij --live: schrijf de ruwe inkomende ASCII-regels ook weg naar dit bestand "
        "(toevoegend), zodat je naast live-verwerking ook een logbestand overhoudt",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=Path("logboek.csv"), help="Pad naar het CSV-bestand (standaard: logboek.csv)"
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="Startdatum YYYY-MM-DD voor het eerste logbestand (anders geraden uit bestandsnaam of wijzigingsdatum). "
        "Niet van toepassing bij --live of .ebl-bestanden (die halen hun datum/tijd uit de data zelf).",
    )
    parser.add_argument(
        "--speed-threshold-kn",
        type=float,
        default=0.5,
        help="Vaart (kn) onder deze grens telt als 'stilliggend' (standaard 0.5)",
    )
    parser.add_argument(
        "--min-stop-minutes",
        type=float,
        default=10.0,
        help="Minimale duur (minuten) van stilliggen om als haventoegang te tellen (standaard 10)",
    )
    parser.add_argument(
        "--no-geocode",
        action="store_true",
        help="Sla online havennaam-opzoeking over; toont coördinaten in plaats van namen",
    )
    parser.add_argument(
        "--cache-file",
        type=Path,
        default=Path(".geocode_cache.json"),
        help="Cachebestand voor havennamen (standaard .geocode_cache.json)",
    )
    parser.add_argument("--language", type=str, default="nl", help="Taal voor havennamen (standaard nl)")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if bool(args.logfiles) == bool(args.live):
        parser.error("geef óf één of meer logfiles óf --live HOST[:PORT] op (niet beide, niet geen van beide)")
    if args.duration is not None and not args.live:
        parser.error("--duration is alleen van toepassing samen met --live")
    if args.tee is not None and not args.live:
        parser.error("--tee is alleen van toepassing samen met --live")

    fixes_by_source: Dict[int, List[PositionFix]] = {}
    sogs_by_source: Dict[int, List[SogSample]] = {}
    depth_by_source: Dict[int, List[DepthSample]] = {}
    all_engine: List[EngineSample] = []
    all_trip_fuel: List[TripFuelSample] = []

    if args.live:
        host, port = _parse_host_port(args.live, DEFAULT_PORT)
        print(f"Live verbinden met {host}:{port}... (Ctrl+C om te stoppen)", file=sys.stderr)
        try:
            frames = iter_frames_tcp(host, port, tee_to=args.tee)
        except OSError as exc:
            print(f"Kon niet verbinden met {host}:{port}: {exc}", file=sys.stderr)
            return 1
        deadline = time.monotonic() + args.duration if args.duration else None
        fixes_by_source, sogs_by_source, all_engine, all_trip_fuel, depth_by_source = _collect_samples(
            frames, deadline=deadline
        )
    else:
        start_date = date.fromisoformat(args.start_date) if args.start_date else None
        for index, path in enumerate(args.logfiles):
            if not path.exists():
                print(f"Logbestand niet gevonden: {path}", file=sys.stderr)
                return 1
            frames = _iter_frames_for_path(path, start_date if index == 0 else None)
            fixes, sogs, engine, trip_fuel, depth = _collect_samples(frames)
            _merge_by_source(fixes_by_source, fixes)
            _merge_by_source(sogs_by_source, sogs)
            all_engine += engine
            all_trip_fuel += trip_fuel
            _merge_by_source(depth_by_source, depth)

    all_fixes, all_sogs, primary_gps_source = _select_primary_gps_source(fixes_by_source, sogs_by_source)
    all_depth = _dominant_source_only(depth_by_source)

    if len(fixes_by_source) > 1:
        print(
            f"Meerdere positiebronnen gevonden ({sorted(fixes_by_source)}); "
            f"bron {primary_gps_source} gebruikt als primaire GPS (meeste berichten).",
            file=sys.stderr,
        )

    if not all_fixes:
        print("Geen positiedata (PGN 129025) gevonden.", file=sys.stderr)
        return 1

    geocoder = NoGeocoder() if args.no_geocode else Geocoder(cache_file=args.cache_file, language=args.language)

    trips = build_trips(
        all_fixes,
        all_sogs,
        all_engine,
        all_trip_fuel,
        all_depth,
        geocoder=geocoder,
        speed_threshold_kn=args.speed_threshold_kn,
        min_stop_minutes=args.min_stop_minutes,
    )

    if not trips:
        print(
            "Geen reizen gevonden (misschien nooit lang genoeg gestopt of gevaren t.o.v. de drempels).",
            file=sys.stderr,
        )
        return 1

    write_csv(trips, args.output)
    gpx_path = args.output.with_suffix(".gpx")
    write_gpx(trips, gpx_path)
    print(f"Logboek geschreven: {args.output} ({len(trips)} reis/reizen)")
    print(f"Route geschreven: {gpx_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
