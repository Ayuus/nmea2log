from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

from .ascii_reader import iter_frames
from .geocode import Geocoder, NoGeocoder
from .logbook_writer import write_csv
from .model import EngineSample, PositionFix, SogSample
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
    frames,
) -> Tuple[List[PositionFix], List[SogSample], List[EngineSample]]:
    fixes: List[PositionFix] = []
    sogs: List[SogSample] = []
    engine_samples: List[EngineSample] = []
    for frame in frames:
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
    return fixes, sogs, engine_samples


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nmea2000logboek",
        description="Zet Actisense N2K ASCII-logbestanden (W2K-2) om in een vaarlogboek (CSV).",
    )
    parser.add_argument(
        "logfiles", nargs="+", type=Path, help="Eén of meer .raw/.n2k-logbestanden (N2K ASCII-formaat)"
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=Path("logboek.csv"), help="Pad naar het CSV-bestand (standaard: logboek.csv)"
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="Startdatum YYYY-MM-DD voor het eerste logbestand (anders geraden uit bestandsnaam of wijzigingsdatum)",
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
    args = build_arg_parser().parse_args(argv)
    start_date = date.fromisoformat(args.start_date) if args.start_date else None

    all_fixes: List[PositionFix] = []
    all_sogs: List[SogSample] = []
    all_engine: List[EngineSample] = []

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
        print("Geen positiedata (PGN 129025) gevonden in de opgegeven bestanden.", file=sys.stderr)
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
    print(f"Logboek geschreven: {args.output} ({len(trips)} reis/reizen)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
