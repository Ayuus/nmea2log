"""The attitude samples (pitch/roll, PGN 127257) of a season, without holding them all in memory.

A real season has ~37 million of these rows -- 600 MB even as float32 columns, more than everything
else together -- yet only the trips use them (spread and range of roll and pitch per trip: ~43 hours in
all). So the pipeline keeps, per file, only what it takes to find the rows again (which sources sent
attitude, how many samples, what time span) and hands build_trips() an AttitudeSegments object whose
``between(start, end)`` reads back just the files that overlap one trip's window, from the sample cache.

Without a sample cache (the CLI with caching off, tests) there is nothing to read back from, so a file's
samples stay in memory, as before: the whole season is resident then, but nothing else changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .fix_array import AttitudeArray, log_time_anomaly
from .log import log
from .model import AttitudeSample
from .sample_cache import AttitudeSummary, SampleCache, summarize_attitude


@dataclass
class _Segment:
    """One file's attitude samples: what they cover per source, and where they are."""

    path: Path
    summary: Dict[int, AttitudeSummary]
    in_memory: Optional[Dict[int, AttitudeArray]]  # None: they are in the sample cache
    time_state_before: object  # the PGN 126992 time state before the file, to decode it again with


class AttitudeSegments:
    """Builds up during decode (add_from_cache / add_decoded, in file order, which is time order),
    then ``dominant_source()`` picks the source most messages came from -- across all files together,
    like every other multi-source PGN (see pipeline._dominant_source_only) -- and returns the object
    build_trips() takes as its attitude samples."""

    def __init__(
        self,
        sample_cache: Optional[SampleCache],
        redecode: Optional[Callable[[Path, object], Dict[int, list]]] = None,
    ) -> None:
        """``redecode(path, time_state_before)`` decodes one file again (its attitude samples, by source);
        used when a file's entry is gone from the sample cache by the time a trip needs it."""
        self._sample_cache = sample_cache
        self._redecode = redecode
        self._segments: List[_Segment] = []
        self._counts: Dict[int, int] = {}

    def add_from_cache(self, path: Path, summary: Dict[int, AttitudeSummary], time_state_before: object = None) -> None:
        """A file whose entry is in the sample cache: only its summary is kept."""
        self._segments.append(_Segment(path, summary, None, time_state_before))
        self._count(summary)

    def add_decoded(
        self, path: Path, by_source: Dict[int, list], *, stored_in_cache: bool, time_state_before: object = None
    ) -> None:
        """A file that was just decoded. With ``stored_in_cache`` (its entry was written to the sample
        cache) only its summary is kept; otherwise its samples stay in memory."""
        summary = summarize_attitude(by_source)
        in_memory = None
        if not stored_in_cache:
            in_memory = {source: _sorted_array(items) for source, items in by_source.items() if items}
        self._segments.append(_Segment(path, summary, in_memory, time_state_before))
        self._count(summary)

    def _count(self, summary: Dict[int, AttitudeSummary]) -> None:
        for source, info in summary.items():
            self._counts[source] = self._counts.get(source, 0) + info.count

    def dominant_source(self) -> "SourceAttitude":
        source = max(self._counts, key=self._counts.get) if self._counts else None
        if source is not None:
            self._report_time_anomalies(source)
        return SourceAttitude(self._segments, source, self._sample_cache, self._redecode)

    def _report_time_anomalies(self, source: int) -> None:
        """Loud, once for the season, like AttitudeArray.drop_time_regressions was when it saw the whole
        array: rows that go back in time inside a file, and files that start before the previous one
        ended. The windows read later repair such rows silently (see SourceAttitude.between)."""
        infos = [segment.summary[source] for segment in self._segments if source in segment.summary]
        dropped = sum(info.dropped for info in infos)
        resets = sum(info.clock_resets for info in infos)
        seam_overlaps = sum(
            1 for previous, current in zip(infos, infos[1:]) if current.first_time < previous.last_time
        )
        if dropped or resets:
            first = min((info.first_dropped_at for info in infos if info.first_dropped_at is not None), default=None)
            log_time_anomaly(
                AttitudeArray._LABEL, dropped, max((info.max_backward_s for info in infos), default=0.0), first, resets
            )
        if seam_overlaps:
            log(
                f"[anomaly] {AttitudeArray._LABEL}: {seam_overlaps} file(s) start before the previous file ended -- "
                "the source data is not in time order across files. This should not happen and needs "
                "investigating; the affected rows were repaired, not trusted."
            )


def _sorted_array(items: List[AttitudeSample]) -> AttitudeArray:
    return AttitudeArray(items).drop_time_regressions(report=False)


class SourceAttitude:
    """The attitude samples of one source over the whole decoded window, read per time window."""

    def __init__(
        self,
        segments: List[_Segment],
        source: Optional[int],
        sample_cache: Optional[SampleCache],
        redecode: Optional[Callable[[Path, object], Dict[int, list]]],
    ) -> None:
        self._segments = segments
        self._source = source
        self._sample_cache = sample_cache
        self._redecode = redecode

    def __len__(self) -> int:
        if self._source is None:
            return 0
        return sum(segment.summary[self._source].count for segment in self._segments if self._source in segment.summary)

    def between(self, start: datetime, end: datetime) -> AttitudeArray:
        """The samples with ``start <= time <= end``, in time order (rows that go back in time are
        dropped, like build_trips() does for a whole array)."""
        window = AttitudeArray()
        if self._source is None:
            return window
        for segment in self._segments:
            info = segment.summary.get(self._source)
            if info is None or info.last_time < start or info.first_time > end:
                continue
            if segment.in_memory is not None:
                window.extend_array(segment.in_memory[self._source].between(start, end))
                continue
            by_source = self._sample_cache.get_attitude(segment.path) if self._sample_cache is not None else None
            if by_source is None and self._redecode is not None:
                # The entry is gone (deleted, or written by another version): decode the file again.
                log(f"[info] Decoding {segment.path.name} again for its attitude samples (not in the sample cache any more).")
                by_source = self._redecode(segment.path, segment.time_state_before)
            if by_source is None:
                log(f"[anomaly] The attitude samples of {segment.path.name} are not in the sample cache and cannot be "
                    "decoded again; the trip statistics that use them leave that file out.")
                continue
            window.extend(item for item in by_source.get(self._source, ()) if start <= item.time <= end)
        # Silent: the season-wide report above already said whatever there was to say.
        return window.drop_time_regressions(report=False)
