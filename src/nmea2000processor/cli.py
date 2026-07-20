from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from .ascii_reader import iter_frames
from .geocode import Geocoder, NoGeocoder
from .gpx_writer import write_gpx
from .logbook_writer import write_csv
from .model import EngineSample, Frame, PositionFix, SogSample
from .network_reader import DEFAULT_PORT, iter_frames_tcp
from .pgn_decode import (
    PGN_COG_SOG_RAPID,
    PGN_ENGINE_DYNAMIC,
    PGN_POSITION_RAPID,
    decode_engine_dynamic,
    decode_position_rapid,
    decode_sog,
)
from .tripbuilder import build_trips


def _collect_samples(
    frames: Iterable[Frame], *, deadline: Optional[float] = None
) -> Tuple[List[PositionFix], List[SogSample], List[EngineSample]]:
    """Verwerkt frames tot samples. Stopt netjes op Ctrl+C of als de deadline verstrijkt,
    zodat een live-sessie altijd een logboek oplevert van wat er tot dan toe binnen is."""
    fixes: List[PositionFix] = []
    sogs: List[SogSample] = []
    engine_samples: List[EngineSample] = []
    try:
        for frame in frames:
            if deadline is not None and time.monotonic() >= deadline:
                print("Duur verstreken; live-sessie wordt afgesloten...", file=sys.stderr)
                break
            if frame.pgn == PGN_POSITION_RAPID:
                decoded = decode_position_rapid(frame.data)
                if decoded is not None:
                    lat, lon = decoded
                    fixes.append(PositionFix(frame.time, lat, lon))
            elif frame.pgn == PGN_COG_SOG_RAPID:
                sog = decode_sog(frame.data)
                if sog is not None:
                    sogs.append(SogSample(frame.time, sog))
            elif frame.pgn == PGN_ENGINE_DYNAMIC:
                decoded = decode_engine_dynamic(frame.data)
                if decoded is not None:
                    instance, fuel_lph, hours_s = decoded
                    engine_samples.append(EngineSample(frame.time, instance, fuel_lph, hours_s))
    except KeyboardInterrupt:
        print("\nOnderbroken door gebruiker; logboek wordt geschreven met de tot nu toe verzamelde data...", file=sys.stderr)
    return fixes, sogs, engine_samples


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
        help="Eén of meer .raw/.n2k-logbestanden (N2K ASCII-formaat). Niet combineren met --live.",
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
        "Niet van toepassing bij --live (daar geldt de huidige datum).",
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

    all_fixes: List[PositionFix] = []
    all_sogs: List[SogSample] = []
    all_engine: List[EngineSample] = []

    if args.live:
        host, port = _parse_host_port(args.live, DEFAULT_PORT)
        print(f"Live verbinden met {host}:{port}... (Ctrl+C om te stoppen)", file=sys.stderr)
        try:
            frames = iter_frames_tcp(host, port, tee_to=args.tee)
        except OSError as exc:
            print(f"Kon niet verbinden met {host}:{port}: {exc}", file=sys.stderr)
            return 1
        deadline = time.monotonic() + args.duration if args.duration else None
        all_fixes, all_sogs, all_engine = _collect_samples(frames, deadline=deadline)
    else:
        start_date = date.fromisoformat(args.start_date) if args.start_date else None
        for index, path in enumerate(args.logfiles):
            if not path.exists():
                print(f"Logbestand niet gevonden: {path}", file=sys.stderr)
                return 1
            frames = iter_frames(path, start_date=start_date if index == 0 else None)
            fixes, sogs, engine = _collect_samples(frames)
            all_fixes += fixes
            all_sogs += sogs
            all_engine += engine

    if not all_fixes:
        print("Geen positiedata (PGN 129025) gevonden.", file=sys.stderr)
        return 1

    geocoder = NoGeocoder() if args.no_geocode else Geocoder(cache_file=args.cache_file, language=args.language)

    trips = build_trips(
        all_fixes,
        all_sogs,
        all_engine,
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
