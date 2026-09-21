"""The decisions of the "on the boat" mode, as a plain state machine with no Android in it: events go
in (a timer fired, the W2K-2 was found or not, a round or a publish finished), actions come out
(probe the W2K-2, run a round, publish, schedule the next tick, tell the user). Time is always passed
in with the event (epoch milliseconds), never read from a clock, so every rule can be tested
deterministically. The Android service that owns the timer, runs the rounds and shows the
notification just carries the actions out, calling ``step()`` with the persisted state -- see there.

The flow, in short:

- OFF -> Start -> SEARCHING: probe for the W2K-2 every few minutes.
- W2K-2 found -> ABOARD: a round (download + build) right away, then one every interval.
- After a round: if the boat lies in a harbour (stationary long enough, engine off long enough) it is
  the final round -> publish -> IDLE ("waiting in port").
- A round that cannot reach the W2K-2 for long enough (the user left the boat) -> publish what is there
  (only if something changed since the last publish) -> IDLE.
- IDLE: look every few minutes whether the W2K-2 is back with new files; then ABOARD again.
- A failed publish (no internet at sea) is retried until it works.

Rounds always download *and* build: the harbour is recognised from the boat's state at the end of the
data (``BoatState`` in tripbuilder.py), which only exists after a build.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields, replace
from enum import Enum
from typing import Dict, List, Optional, Union


class Phase(str, Enum):
    OFF = "OFF"
    SEARCHING = "SEARCHING"
    ABOARD = "ABOARD"
    IDLE = "IDLE"


class Work(str, Enum):
    """What is running right now, so a tick that arrives meanwhile is postponed instead of doubled."""

    ROUND = "ROUND"
    PUBLISH = "PUBLISH"


class Status(str, Enum):
    """What the user should be told; the UI turns these into (translated) text."""

    SEARCHING = "SEARCHING"
    ROUND_STARTED = "ROUND_STARTED"
    ROUND_DONE = "ROUND_DONE"
    ROUND_FAILED = "ROUND_FAILED"
    W2K_NOT_FOUND_RETRY = "W2K_NOT_FOUND_RETRY"
    HARBOUR_FINAL = "HARBOUR_FINAL"
    LEFT_BOAT = "LEFT_BOAT"
    LEFT_BOAT_NOTHING_TO_PUBLISH = "LEFT_BOAT_NOTHING_TO_PUBLISH"
    WAITING_IN_PORT = "WAITING_IN_PORT"
    PUBLISH_STARTED = "PUBLISH_STARTED"
    PUBLISH_OK = "PUBLISH_OK"
    PUBLISH_FAILED = "PUBLISH_FAILED"
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class BootModeConfig:
    round_interval_minutes: int = 60
    publish_every_round: bool = False
    final_on_harbour: bool = True
    harbour_stationary_minutes: int = 30
    harbour_engine_off_minutes: int = 10
    final_on_left_boat: bool = True
    left_boat_minutes: int = 20
    stop_after_final: bool = False
    publish_configured: bool = True  # WordPress or SFTP is filled in; without it the mode still builds
    search_interval_minutes: int = 5
    port_poll_minutes: int = 15
    publish_retry_minutes: int = 15
    failed_round_retry_minutes: int = 5

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "BootModeConfig":
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})  # type: ignore[arg-type]


@dataclass(frozen=True)
class BoatSnapshot:
    """``tripbuilder.BoatState.to_dict()`` as of the end of the data just processed."""

    underway: bool
    stationary_since: Optional[str]  # start of the trailing stay (ISO text): identifies *which* stay
    stationary_seconds: Optional[int]
    engine_running: Optional[bool]  # None: no engine data at all
    engine_off_seconds: Optional[int]

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "BoatSnapshot":
        """From BoatState.to_dict() (extra keys such as the position are ignored)."""
        return cls(
            underway=bool(data["underway"]),
            stationary_since=data.get("stationary_since"),  # type: ignore[arg-type]
            stationary_seconds=data.get("stationary_seconds"),  # type: ignore[arg-type]
            engine_running=data.get("engine_running"),  # type: ignore[arg-type]
            engine_off_seconds=data.get("engine_off_seconds"),  # type: ignore[arg-type]
        )

    def is_in_harbour(self, config: BootModeConfig) -> bool:
        """Not underway, stationary for at least the configured time, and the engine off for at least
        its configured time -- or no engine data at all (a boat without engine PGNs), in which case
        only the stationary time counts."""
        if self.underway:
            return False
        if (self.stationary_seconds or 0) < config.harbour_stationary_minutes * 60:
            return False
        if self.engine_running is None:
            return True
        if self.engine_running:
            return False
        return (self.engine_off_seconds or 0) >= config.harbour_engine_off_minutes * 60


# --- outcomes of a round ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoundOk:
    downloaded_count: int
    boat: Optional[BoatSnapshot]


@dataclass(frozen=True)
class RoundNotFound:
    pass


@dataclass(frozen=True)
class RoundFailed:
    message: str


RoundOutcome = Union[RoundOk, RoundNotFound, RoundFailed]


# --- events ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Start:
    at: int


@dataclass(frozen=True)
class Stop:
    at: int


@dataclass(frozen=True)
class Tick:
    """The timer scheduled by ScheduleTick fired. ``busy``: the user has a run going of their own."""

    at: int
    busy: bool = False


@dataclass(frozen=True)
class ProbeResult:
    """Answer to ProbeW2k: is the W2K-2 reachable, and does it hold files not downloaded yet."""

    at: int
    found: bool
    has_new_files: bool = False


@dataclass(frozen=True)
class RoundFinished:
    at: int
    outcome: RoundOutcome


@dataclass(frozen=True)
class PublishFinished:
    at: int
    ok: bool


Event = Union[Start, Stop, Tick, ProbeResult, RoundFinished, PublishFinished]


# --- actions ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduleTick:
    """Wake the machine with a Tick at ``at``; replaces any earlier schedule, None cancels it."""

    at: Optional[int]


@dataclass(frozen=True)
class ProbeW2k:
    """A cheap look at whether the W2K-2 answers (and holds new files); answer with ProbeResult."""


@dataclass(frozen=True)
class StartRound:
    """Download the new files and build the logbook; answer with RoundFinished."""


@dataclass(frozen=True)
class Publish:
    """Publish the logbook as it was last built; answer with PublishFinished."""


@dataclass(frozen=True)
class Notify:
    kind: Status
    next_at: Optional[int] = None


@dataclass(frozen=True)
class StopService:
    """The mode is over: stop the foreground service."""


Action = Union[ScheduleTick, ProbeW2k, StartRound, Publish, Notify, StopService]


# --- state -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BootState:
    phase: Phase = Phase.OFF
    working: Optional[Work] = None
    last_w2k_seen_at: Optional[int] = None
    # Start of the current unbroken run of "W2K-2 not reachable" rounds; None while it answers.
    first_miss_at: Optional[int] = None
    last_round_at: Optional[int] = None
    # The logbook changed since it was last published (or was never published in this session).
    dirty_since_publish: bool = False
    publish_pending: bool = False
    last_final_stay: Optional[str] = None  # the stay (BoatSnapshot.stationary_since) a final round was done for
    final_triggered: bool = False

    def to_dict(self) -> Dict[str, object]:
        data = asdict(self)
        data["phase"] = self.phase.value
        data["working"] = self.working.value if self.working else None
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "BootState":
        known = {f.name for f in fields(cls)}
        values = {key: value for key, value in data.items() if key in known}
        values["phase"] = Phase(values.get("phase", "OFF"))
        working = values.get("working")
        values["working"] = Work(working) if working else None
        return cls(**values)  # type: ignore[arg-type]


_MINUTE_MS = 60_000
_BUSY_RETRY_MS = 60_000


def _minutes(n: int) -> int:
    return n * _MINUTE_MS


class BootModeMachine:
    def __init__(self, config: Optional[BootModeConfig] = None, state: Optional[BootState] = None) -> None:
        self.config = config or BootModeConfig()
        self.state = state or BootState()

    def handle(self, event: Event) -> List[Action]:
        if isinstance(event, Start):
            return self._on_start()
        if isinstance(event, Stop):
            return self._on_stop()
        if isinstance(event, Tick):
            return self._on_tick(event)
        if isinstance(event, ProbeResult):
            return self._on_probe(event)
        if isinstance(event, RoundFinished):
            return self._on_round(event)
        if isinstance(event, PublishFinished):
            return self._on_published(event)
        raise TypeError(f"unknown event {event!r}")

    # -- events

    def _on_start(self) -> List[Action]:
        if self.state.phase is not Phase.OFF:
            return []
        # Dirty from the start: the first final publish of a session always goes out once.
        self.state = BootState(phase=Phase.SEARCHING, dirty_since_publish=True)
        return [Notify(Status.SEARCHING), ProbeW2k()]

    def _on_stop(self) -> List[Action]:
        if self.state.phase is Phase.OFF:
            return []
        self.state = BootState()
        return [ScheduleTick(None), Notify(Status.STOPPED), StopService()]

    def _on_tick(self, event: Tick) -> List[Action]:
        if self.state.phase is Phase.OFF:
            return []
        if event.busy or self.state.working is not None:
            return [ScheduleTick(event.at + _BUSY_RETRY_MS)]
        if self.state.publish_pending:
            return self._start_publish()
        if self.state.phase is Phase.ABOARD:
            return self._start_round()
        return [ProbeW2k()]  # SEARCHING or IDLE

    def _on_probe(self, event: ProbeResult) -> List[Action]:
        if self.state.phase is Phase.OFF or self.state.working is not None:
            return []
        at, config = event.at, self.config
        if self.state.phase is Phase.SEARCHING:
            if event.found:
                self.state = replace(self.state, phase=Phase.ABOARD, last_w2k_seen_at=at, first_miss_at=None)
                return self._start_round()
            return [ScheduleTick(at + _minutes(config.search_interval_minutes))]
        if self.state.phase is Phase.IDLE:
            if event.found:
                self.state = replace(self.state, last_w2k_seen_at=at, first_miss_at=None)
                if event.has_new_files:
                    self.state = replace(self.state, phase=Phase.ABOARD, final_triggered=False)
                    return self._start_round()
            return [ScheduleTick(at + _minutes(config.port_poll_minutes))]
        return []

    def _on_round(self, event: RoundFinished) -> List[Action]:
        if self.state.phase is Phase.OFF:
            return []  # a late result after the mode was stopped
        self.state = replace(self.state, working=None)
        outcome, at = event.outcome, event.at
        if isinstance(outcome, RoundOk):
            return self._on_round_ok(at, outcome)
        if isinstance(outcome, RoundNotFound):
            return self._on_round_not_found(at)
        return [Notify(Status.ROUND_FAILED), ScheduleTick(at + _minutes(self.config.failed_round_retry_minutes))]

    def _on_round_ok(self, at: int, outcome: RoundOk) -> List[Action]:
        config = self.config
        self.state = replace(
            self.state,
            last_w2k_seen_at=at,
            first_miss_at=None,
            last_round_at=at,
            dirty_since_publish=self.state.dirty_since_publish or outcome.downloaded_count > 0,
        )
        boat = outcome.boat
        if config.final_on_harbour and boat is not None and boat.is_in_harbour(config):
            if boat.stationary_since != self.state.last_final_stay:
                # A new stay in a harbour: this is the final round.
                self.state = replace(
                    self.state, phase=Phase.IDLE, last_final_stay=boat.stationary_since, final_triggered=True
                )
                return self._final_round(at, Status.HARBOUR_FINAL, always_publish=True)
            # The stay was published already; the W2K-2 just keeps logging in port.
            self.state = replace(self.state, phase=Phase.IDLE)
            next_at = at + _minutes(config.port_poll_minutes)
            return [Notify(Status.WAITING_IN_PORT, next_at), ScheduleTick(next_at)]
        self.state = replace(self.state, phase=Phase.ABOARD)
        actions: List[Action] = [Notify(Status.ROUND_DONE)]
        if config.publish_every_round and config.publish_configured and outcome.downloaded_count > 0:
            actions += self._start_publish()
        else:
            actions.append(ScheduleTick(self._next_tick_after(at)))
        return actions

    def _on_round_not_found(self, at: int) -> List[Action]:
        config = self.config
        # Measured from the first miss of an unbroken run, not from the last success: rounds are an
        # interval apart, so one failed attempt after a long gap must not count as "left the boat".
        first_miss = self.state.first_miss_at if self.state.first_miss_at is not None else at
        self.state = replace(self.state, first_miss_at=first_miss)
        if config.final_on_left_boat and at - first_miss >= _minutes(config.left_boat_minutes):
            self.state = replace(self.state, phase=Phase.IDLE, final_triggered=True)
            return self._final_round(at, Status.LEFT_BOAT, always_publish=False)
        retry_at = at + _minutes(min(config.search_interval_minutes, config.round_interval_minutes))
        return [Notify(Status.W2K_NOT_FOUND_RETRY, retry_at), ScheduleTick(retry_at)]

    def _final_round(self, at: int, kind: Status, always_publish: bool) -> List[Action]:
        """The last round of a stay: publish (when a destination is set) unless nothing changed since
        the last publish -- the harbour case always publishes, having just built fresh data."""
        config = self.config
        needs_publish = config.publish_configured and (
            always_publish or self.state.dirty_since_publish or self.state.publish_pending
        )
        if needs_publish:
            return [Notify(kind), *self._start_publish()]
        status = Status.LEFT_BOAT_NOTHING_TO_PUBLISH if kind is Status.LEFT_BOAT and config.publish_configured else kind
        if config.stop_after_final:
            return self._stop_after_final(status)
        return [Notify(status), ScheduleTick(at + _minutes(config.port_poll_minutes))]

    def _on_published(self, event: PublishFinished) -> List[Action]:
        if self.state.phase is Phase.OFF:
            return []
        at, config = event.at, self.config
        self.state = replace(self.state, working=None)
        if not event.ok:
            retry_at = at + _minutes(config.publish_retry_minutes)
            return [Notify(Status.PUBLISH_FAILED, retry_at), ScheduleTick(retry_at)]
        self.state = replace(self.state, publish_pending=False, dirty_since_publish=False)
        if self.state.final_triggered and config.stop_after_final:
            return self._stop_after_final(Status.PUBLISH_OK)
        return [Notify(Status.PUBLISH_OK), ScheduleTick(self._next_tick_after(at))]

    # -- helpers

    def _stop_after_final(self, status: Status) -> List[Action]:
        self.state = BootState()
        return [ScheduleTick(None), Notify(status), Notify(Status.STOPPED), StopService()]

    def _start_round(self) -> List[Action]:
        self.state = replace(self.state, working=Work.ROUND)
        return [Notify(Status.ROUND_STARTED), StartRound()]

    def _start_publish(self) -> List[Action]:
        self.state = replace(self.state, working=Work.PUBLISH, publish_pending=True)
        return [Notify(Status.PUBLISH_STARTED), Publish()]

    def _next_tick_after(self, at: int) -> int:
        """When the timer should fire next, once the current work is done, for the phase we are in."""
        config, state = self.config, self.state
        if state.publish_pending:
            return at + _minutes(config.publish_retry_minutes)
        if state.phase is Phase.SEARCHING:
            return at + _minutes(config.search_interval_minutes)
        if state.phase is Phase.ABOARD:
            return max(at, (state.last_round_at if state.last_round_at is not None else at) + _minutes(config.round_interval_minutes))
        return at + _minutes(config.port_poll_minutes)


# --- the boundary the Android service calls: JSON in, JSON out ---------------------------------------


def _event_from_dict(data: Dict[str, object]) -> Event:
    kind = data["type"]
    at = int(data["at"])  # type: ignore[arg-type]
    if kind == "start":
        return Start(at)
    if kind == "stop":
        return Stop(at)
    if kind == "tick":
        return Tick(at, bool(data.get("busy", False)))
    if kind == "probe":
        return ProbeResult(at, bool(data["found"]), bool(data.get("has_new_files", False)))
    if kind == "round":
        result = data["result"]
        if result == "ok":
            boat = data.get("boat")
            return RoundFinished(
                at, RoundOk(int(data.get("downloaded_count", 0)), BoatSnapshot.from_dict(boat) if boat else None)  # type: ignore[arg-type]
            )
        if result == "not_found":
            return RoundFinished(at, RoundNotFound())
        return RoundFinished(at, RoundFailed(str(data.get("message", ""))))
    if kind == "publish":
        return PublishFinished(at, bool(data["ok"]))
    raise ValueError(f"unknown event type {kind!r}")


def _action_to_dict(action: Action) -> Dict[str, object]:
    if isinstance(action, ScheduleTick):
        return {"type": "schedule_tick", "at": action.at}
    if isinstance(action, ProbeW2k):
        return {"type": "probe_w2k"}
    if isinstance(action, StartRound):
        return {"type": "start_round"}
    if isinstance(action, Publish):
        return {"type": "publish"}
    if isinstance(action, Notify):
        return {"type": "notify", "kind": action.kind.value, "next_at": action.next_at}
    return {"type": "stop_service"}


def step(state_json: Optional[str], config_json: str, event_json: str) -> str:
    """One step of the machine for the Android side, all as JSON text (no Python objects cross the
    boundary): the persisted state (None/empty for a fresh start), the current config and one event
    (``{"type": "tick", "at": ms, ...}``, see _event_from_dict). Returns ``{"state": {...},
    "actions": [{"type": ..., ...}, ...]}``; persist the state and carry out the actions."""
    machine = BootModeMachine(
        BootModeConfig.from_dict(json.loads(config_json)),
        BootState.from_dict(json.loads(state_json)) if state_json else BootState(),
    )
    actions = machine.handle(_event_from_dict(json.loads(event_json)))
    return json.dumps({"state": machine.state.to_dict(), "actions": [_action_to_dict(a) for a in actions]})
