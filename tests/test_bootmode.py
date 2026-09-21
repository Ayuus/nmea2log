import json

import pytest

from nmea2log.bootmode import (
    BoatSnapshot,
    BootModeConfig,
    BootModeMachine,
    BootState,
    Notify,
    Phase,
    ProbeResult,
    ProbeW2k,
    Publish,
    PublishFinished,
    RoundFailed,
    RoundFinished,
    RoundNotFound,
    RoundOk,
    Resume,
    ScheduleTick,
    Start,
    StartRound,
    Status,
    Stop,
    StopService,
    Tick,
    Work,
    step,
)

T0 = 1_000_000_000_000


def minutes(n: int) -> int:
    return n * 60_000


def harbour_boat(since: str = "2026-09-12T12:18:18", stationary_min: int = 45, engine_off_min: int = 20) -> BoatSnapshot:
    return BoatSnapshot(
        underway=False,
        stationary_since=since,
        stationary_seconds=stationary_min * 60,
        engine_running=False,
        engine_off_seconds=engine_off_min * 60,
    )


UNDERWAY = BoatSnapshot(underway=True, stationary_since=None, stationary_seconds=None, engine_running=True, engine_off_seconds=None)


def machine(**config) -> BootModeMachine:
    return BootModeMachine(BootModeConfig(**config))


def aboard(m: BootModeMachine, at: int = T0):
    """Start, find the W2K-2 at once; the first round is running."""
    m.handle(Start(at))
    return m.handle(ProbeResult(at, found=True))


def finish_round(m: BootModeMachine, at: int, boat=UNDERWAY, downloaded: int = 3):
    return m.handle(RoundFinished(at, RoundOk(downloaded, boat)))


def kinds(actions):
    return [a.kind for a in actions if isinstance(a, Notify)]


# --- starting and searching ------------------------------------------------------------------------


def test_start_looks_for_the_w2k2():
    m = machine()

    actions = m.handle(Start(T0))

    assert actions == [Notify(Status.SEARCHING), ProbeW2k()]
    assert m.state.phase is Phase.SEARCHING


def test_starting_twice_does_nothing():
    m = machine()
    m.handle(Start(T0))

    assert m.handle(Start(T0 + 1)) == []


def test_searching_probes_again_after_the_search_interval():
    m = machine()
    m.handle(Start(T0))

    assert m.handle(ProbeResult(T0, found=False)) == [ScheduleTick(T0 + minutes(5))]
    assert m.handle(Tick(T0 + minutes(5))) == [ProbeW2k()]


def test_finding_the_w2k2_starts_the_first_round_at_once():
    m = machine()
    m.handle(Start(T0))

    actions = m.handle(ProbeResult(T0, found=True))

    assert actions == [Notify(Status.ROUND_STARTED), StartRound()]
    assert m.state.phase is Phase.ABOARD and m.state.working is Work.ROUND


# --- rounds ------------------------------------------------------------------------------------------


def test_a_round_while_underway_schedules_the_next_one_an_interval_later():
    m = machine()
    aboard(m)

    actions = finish_round(m, T0 + minutes(2))

    assert actions == [Notify(Status.ROUND_DONE), ScheduleTick(T0 + minutes(2) + minutes(60))]
    assert m.state.phase is Phase.ABOARD and m.state.working is None


def test_the_tick_after_the_interval_starts_the_next_round():
    m = machine()
    aboard(m)
    finish_round(m, T0)

    assert m.handle(Tick(T0 + minutes(60))) == [Notify(Status.ROUND_STARTED), StartRound()]


def test_a_failed_round_is_retried_soon():
    m = machine()
    aboard(m)

    actions = m.handle(RoundFinished(T0, RoundFailed("boom")))

    assert actions == [Notify(Status.ROUND_FAILED), ScheduleTick(T0 + minutes(5))]
    assert m.state.phase is Phase.ABOARD


def test_a_tick_while_the_user_has_a_run_going_is_postponed():
    m = machine()
    aboard(m)
    finish_round(m, T0)

    assert m.handle(Tick(T0 + minutes(60), busy=True)) == [ScheduleTick(T0 + minutes(60) + 60_000)]


def test_a_tick_while_a_round_is_running_is_postponed_not_doubled():
    m = machine()
    aboard(m)  # the first round is running

    assert m.handle(Tick(T0 + minutes(1))) == [ScheduleTick(T0 + minutes(1) + 60_000)]


# --- the harbour: the final round ---------------------------------------------------------------------


def test_reaching_the_harbour_publishes_and_goes_idle():
    m = machine()
    aboard(m)

    actions = finish_round(m, T0 + minutes(5), boat=harbour_boat())

    assert actions == [Notify(Status.HARBOUR_FINAL), Notify(Status.PUBLISH_STARTED), Publish()]
    assert m.state.phase is Phase.IDLE and m.state.working is Work.PUBLISH
    assert m.state.last_final_stay == "2026-09-12T12:18:18"


def test_after_the_final_publish_the_mode_waits_in_port():
    m = machine()
    aboard(m)
    finish_round(m, T0, boat=harbour_boat())

    actions = m.handle(PublishFinished(T0 + minutes(1), ok=True))

    assert actions == [Notify(Status.PUBLISH_OK), ScheduleTick(T0 + minutes(1) + minutes(15))]
    assert m.state.publish_pending is False and m.state.dirty_since_publish is False


@pytest.mark.parametrize(
    "boat",
    [
        harbour_boat(stationary_min=29),  # not stopped long enough
        harbour_boat(engine_off_min=9),  # engine off too short
        BoatSnapshot(False, "s", 45 * 60, True, None),  # engine still running
        UNDERWAY,
        None,  # no state at all
    ],
)
def test_not_yet_in_the_harbour_keeps_going(boat):
    m = machine()
    aboard(m)

    actions = finish_round(m, T0, boat=boat)

    assert Status.HARBOUR_FINAL not in kinds(actions)
    assert m.state.phase is Phase.ABOARD


def test_no_engine_data_only_needs_the_stationary_time():
    m = machine()
    aboard(m)
    sailboat = BoatSnapshot(False, "2026-09-12T12:18:18", 45 * 60, None, None)

    actions = finish_round(m, T0, boat=sailboat)

    assert Status.HARBOUR_FINAL in kinds(actions)


def test_the_harbour_thresholds_come_from_the_config():
    m = machine(harbour_stationary_minutes=60, harbour_engine_off_minutes=30)
    aboard(m)

    assert Status.HARBOUR_FINAL not in kinds(finish_round(m, T0, boat=harbour_boat(stationary_min=45, engine_off_min=20)))


def test_no_final_round_in_the_harbour_when_that_trigger_is_off():
    m = machine(final_on_harbour=False)
    aboard(m)

    actions = finish_round(m, T0, boat=harbour_boat())

    assert Status.HARBOUR_FINAL not in kinds(actions) and Publish() not in actions
    assert m.state.phase is Phase.ABOARD


def test_the_same_stay_is_not_published_twice():
    """The W2K-2 keeps logging while the boat lies in port: new files, same stay -> no second publish."""
    m = machine()
    aboard(m)
    finish_round(m, T0, boat=harbour_boat())
    m.handle(PublishFinished(T0 + minutes(1), ok=True))
    assert m.handle(Tick(T0 + minutes(16))) == [ProbeW2k()]
    assert m.handle(ProbeResult(T0 + minutes(16), found=True, has_new_files=True)) == [
        Notify(Status.ROUND_STARTED), StartRound()
    ]

    actions = finish_round(m, T0 + minutes(18), boat=harbour_boat(stationary_min=60))

    assert Publish() not in actions
    assert actions == [
        Notify(Status.WAITING_IN_PORT, T0 + minutes(18) + minutes(15)),
        ScheduleTick(T0 + minutes(18) + minutes(15)),
    ]
    assert m.state.phase is Phase.IDLE


def test_a_new_stay_after_sailing_again_is_a_new_final_round():
    m = machine()
    aboard(m)
    finish_round(m, T0, boat=harbour_boat(since="A"))
    m.handle(PublishFinished(T0 + minutes(1), ok=True))
    m.handle(ProbeResult(T0 + minutes(16), found=True, has_new_files=True))
    finish_round(m, T0 + minutes(18), boat=UNDERWAY)  # sailing again: back to aboard
    assert m.state.phase is Phase.ABOARD

    actions = finish_round(m, T0 + minutes(200), boat=harbour_boat(since="B"))

    assert Publish() in actions and m.state.last_final_stay == "B"


def test_without_a_publish_destination_the_final_round_only_reports():
    m = machine(publish_configured=False)
    aboard(m)

    actions = finish_round(m, T0, boat=harbour_boat())

    assert actions == [Notify(Status.HARBOUR_FINAL), ScheduleTick(T0 + minutes(15))]
    assert m.state.phase is Phase.IDLE


# --- publishing ---------------------------------------------------------------------------------------


def test_a_failed_publish_is_retried_until_it_works():
    m = machine()
    aboard(m)
    finish_round(m, T0, boat=harbour_boat())

    actions = m.handle(PublishFinished(T0 + minutes(1), ok=False))

    assert actions == [Notify(Status.PUBLISH_FAILED, T0 + minutes(16)), ScheduleTick(T0 + minutes(16))]
    assert m.state.publish_pending is True
    assert m.handle(Tick(T0 + minutes(16))) == [Notify(Status.PUBLISH_STARTED), Publish()]
    assert m.handle(PublishFinished(T0 + minutes(16), ok=True))[0] == Notify(Status.PUBLISH_OK)
    assert m.state.publish_pending is False


def test_the_mode_can_switch_itself_off_after_the_final_publish():
    m = machine(stop_after_final=True)
    aboard(m)
    finish_round(m, T0, boat=harbour_boat())

    actions = m.handle(PublishFinished(T0 + minutes(1), ok=True))

    assert actions == [ScheduleTick(None), Notify(Status.PUBLISH_OK), Notify(Status.STOPPED), StopService()]
    assert m.state.phase is Phase.OFF


def test_stopping_after_the_final_round_without_a_destination():
    m = machine(publish_configured=False, stop_after_final=True)
    aboard(m)

    actions = finish_round(m, T0, boat=harbour_boat())

    assert actions[-1] == StopService() and m.state.phase is Phase.OFF


def test_publishing_every_round_when_asked_and_something_was_downloaded():
    m = machine(publish_every_round=True)
    aboard(m)

    with_files = finish_round(m, T0, downloaded=4)
    assert Publish() in with_files
    m.handle(PublishFinished(T0 + minutes(1), ok=True))

    assert m.handle(Tick(T0 + minutes(60))) == [Notify(Status.ROUND_STARTED), StartRound()]
    without_files = finish_round(m, T0 + minutes(61), downloaded=0)
    assert Publish() not in without_files


def test_after_a_publish_in_a_round_the_next_round_keeps_its_interval():
    m = machine(publish_every_round=True)
    aboard(m)
    finish_round(m, T0, downloaded=2)

    actions = m.handle(PublishFinished(T0 + minutes(1), ok=True))

    assert actions == [Notify(Status.PUBLISH_OK), ScheduleTick(T0 + minutes(60))]


# --- leaving the boat ----------------------------------------------------------------------------------


def not_found_rounds(m: BootModeMachine, start: int, count: int, gap: int = 5):
    last = []
    for i in range(count):
        last = m.handle(RoundFinished(start + minutes(gap * i), RoundNotFound()))
    return last


def test_a_single_missed_round_after_a_long_gap_is_not_leaving_the_boat():
    """Rounds are an hour apart; one failed attempt must not count as 60 minutes without the W2K-2."""
    m = machine()
    aboard(m)
    finish_round(m, T0)

    actions = m.handle(RoundFinished(T0 + minutes(60), RoundNotFound()))

    assert actions == [Notify(Status.W2K_NOT_FOUND_RETRY, T0 + minutes(65)), ScheduleTick(T0 + minutes(65))]
    assert m.state.phase is Phase.ABOARD


def test_twenty_unbroken_minutes_without_the_w2k2_is_leaving_the_boat():
    m = machine()
    aboard(m)
    finish_round(m, T0)  # downloaded files: the logbook changed since the last publish

    actions = not_found_rounds(m, T0 + minutes(60), count=5)  # misses at +0, +5, +10, +15, +20 min

    assert actions == [Notify(Status.LEFT_BOAT), Notify(Status.PUBLISH_STARTED), Publish()]
    assert m.state.phase is Phase.IDLE


def test_a_found_w2k2_resets_the_time_without_it():
    m = machine()
    aboard(m)
    finish_round(m, T0)
    not_found_rounds(m, T0 + minutes(60), count=3)  # 10 minutes missed
    m.handle(Tick(T0 + minutes(75)))
    finish_round(m, T0 + minutes(76), downloaded=0)  # back for a moment
    assert m.state.first_miss_at is None

    actions = m.handle(RoundFinished(T0 + minutes(140), RoundNotFound()))  # a fresh miss starts a fresh count

    assert Status.LEFT_BOAT not in kinds(actions)


def test_while_waiting_in_port_the_w2k2_going_away_starts_nothing():
    m = idle_machine()

    m.handle(Tick(T0 + minutes(16)))
    actions = m.handle(ProbeResult(T0 + minutes(16), found=False))

    assert actions == [ScheduleTick(T0 + minutes(31))]
    assert m.state.phase is Phase.IDLE and m.state.publish_pending is False


def test_leaving_the_boat_after_everything_was_published_has_nothing_left_to_publish():
    m = machine(publish_every_round=True)
    aboard(m)
    finish_round(m, T0, downloaded=2)  # published right after the round...
    m.handle(PublishFinished(T0 + minutes(1), ok=True))  # ...so nothing is dirty any more
    m.handle(Tick(T0 + minutes(60)))  # the next round starts, but the W2K-2 has gone

    actions = not_found_rounds(m, T0 + minutes(60), count=5)

    assert Status.LEFT_BOAT_NOTHING_TO_PUBLISH in kinds(actions) and Publish() not in actions
    assert m.state.phase is Phase.IDLE


def test_no_final_round_on_leaving_when_that_trigger_is_off():
    m = machine(final_on_left_boat=False)
    aboard(m)
    finish_round(m, T0)

    actions = not_found_rounds(m, T0 + minutes(60), count=8)  # 35 minutes of misses

    assert Publish() not in actions and Status.LEFT_BOAT not in kinds(actions)
    assert m.state.phase is Phase.ABOARD


# --- waiting in port -----------------------------------------------------------------------------------


def idle_machine() -> BootModeMachine:
    m = machine()
    aboard(m)
    finish_round(m, T0, boat=harbour_boat())
    m.handle(PublishFinished(T0 + minutes(1), ok=True))
    assert m.state.phase is Phase.IDLE
    return m


def test_idle_keeps_looking_when_the_w2k2_answers_without_new_files():
    m = idle_machine()

    assert m.handle(ProbeResult(T0 + minutes(16), found=True, has_new_files=False)) == [
        ScheduleTick(T0 + minutes(16) + minutes(15))
    ]


def test_idle_keeps_looking_when_the_w2k2_does_not_answer():
    m = idle_machine()

    assert m.handle(ProbeResult(T0 + minutes(16), found=False)) == [ScheduleTick(T0 + minutes(31))]


def test_idle_starts_rounds_again_when_new_files_appear():
    m = idle_machine()

    actions = m.handle(ProbeResult(T0 + minutes(16), found=True, has_new_files=True))

    assert actions == [Notify(Status.ROUND_STARTED), StartRound()]
    assert m.state.phase is Phase.ABOARD


# --- stopping and late events ------------------------------------------------------------------------


@pytest.mark.parametrize("setup", ["searching", "aboard", "idle"])
def test_stop_from_any_phase_switches_everything_off(setup):
    m = idle_machine() if setup == "idle" else machine()
    if setup == "searching":
        m.handle(Start(T0))
    elif setup == "aboard":
        aboard(m)

    actions = m.handle(Stop(T0 + minutes(3)))

    assert actions == [ScheduleTick(None), Notify(Status.STOPPED), StopService()]
    assert m.state == BootState()


def test_events_after_stopping_are_ignored():
    m = machine()
    aboard(m)
    m.handle(Stop(T0 + 1))

    assert m.handle(RoundFinished(T0 + 2, RoundOk(1, None))) == []
    assert m.handle(PublishFinished(T0 + 3, ok=True)) == []
    assert m.handle(Tick(T0 + 4)) == []
    assert m.handle(ProbeResult(T0 + 5, found=True)) == []


# --- the process was killed and restarted --------------------------------------------------------------


def test_resuming_an_interrupted_round_starts_it_again():
    m = machine()
    aboard(m)  # a round is running, and then the process dies

    actions = m.handle(Resume(T0 + minutes(5)))

    assert actions == [Notify(Status.ROUND_STARTED), StartRound()]
    assert m.state.working is Work.ROUND


def test_resuming_an_interrupted_publish_publishes_again():
    m = machine()
    aboard(m)
    finish_round(m, T0, boat=harbour_boat())  # the final round: the publish is running

    assert m.state.working is Work.PUBLISH
    actions = m.handle(Resume(T0 + minutes(5)))

    assert actions == [Notify(Status.PUBLISH_STARTED), Publish()]
    assert m.state.working is Work.PUBLISH


def test_resuming_while_idle_looks_for_the_w2k2():
    m = idle_machine()

    assert m.handle(Resume(T0 + minutes(90))) == [ProbeW2k()]


def test_resuming_when_the_mode_is_off_does_nothing():
    assert machine().handle(Resume(T0)) == []


# --- the JSON boundary the Android service uses -------------------------------------------------------


def test_state_round_trips_through_a_dict():
    state = BootState(phase=Phase.IDLE, working=Work.PUBLISH, dirty_since_publish=True, last_final_stay="x", first_miss_at=5)

    assert BootState.from_dict(json.loads(json.dumps(state.to_dict()))) == state


def test_step_drives_the_machine_through_json_only():
    config = json.dumps({"round_interval_minutes": 30})

    started = json.loads(step(None, config, json.dumps({"type": "start", "at": T0})))
    assert started["actions"] == [
        {"type": "notify", "kind": "SEARCHING", "next_at": None}, {"type": "probe_w2k"}
    ]

    found = json.loads(step(json.dumps(started["state"]), config, json.dumps({"type": "probe", "at": T0, "found": True})))
    assert found["actions"][-1] == {"type": "start_round"}

    boat = {"underway": True, "stationary_since": None, "stationary_seconds": None, "engine_running": True,
            "engine_off_seconds": None, "latitude": 47.5, "longitude": -2.4}
    done = json.loads(step(json.dumps(found["state"]), config, json.dumps(
        {"type": "round", "at": T0 + 1000, "result": "ok", "downloaded_count": 2, "boat": boat})))
    assert done["actions"][-1] == {"type": "schedule_tick", "at": T0 + 1000 + minutes(30)}  # the config's interval
    assert done["state"]["phase"] == "ABOARD"


def test_step_ignores_config_keys_it_does_not_know():
    config = json.dumps({"round_interval_minutes": 45, "some_future_setting": 1})

    result = json.loads(step(None, config, json.dumps({"type": "start", "at": T0})))

    assert result["state"]["phase"] == "SEARCHING"


def test_boat_snapshot_reads_boatstate_to_dict_output():
    snapshot = BoatSnapshot.from_dict({
        "last_data_at": "2026-09-12T13:01:06", "latitude": 47.5, "longitude": -2.4, "underway": False,
        "stationary_since": "2026-09-12T12:18:18", "stationary_seconds": 2568, "engine_running": False,
        "engine_off_since": "2026-09-12T12:21:28", "engine_off_seconds": 2378,
    })

    assert snapshot.is_in_harbour(BootModeConfig()) is True
    assert snapshot.stationary_since == "2026-09-12T12:18:18"
