"""Baseline: the background job that keeps the disable window >= 6 h away.

`_noon_window_shifter` is an infinite loop, so these tests drive it with a
patched `time.sleep` that advances a scripted clock and stops the loop after a
fixed number of ticks.

The job re-parks the 1-hour disable window twice a day: to 18:00 on the first
tick after midnight, to 06:00 on the first tick after noon (CH-011). Without
the midnight flip, a window parked at 06:00 in the afternoon arrives the next
morning as a real charging hour (CB-3). A half-day only counts as done once the
window is written, so failures are retried every tick (CH-004). Its correctness
is charging behaviour.
"""

import pytest

from conftest import existing_schedule

pytestmark = [pytest.mark.charging, pytest.mark.baseline]


class _LoopFinished(Exception):
    """Breaks out of the shifter's `while True`."""


@pytest.fixture
def run_shifter(server_module, monkeypatch):
    """Run `_noon_window_shifter` over a scripted sequence of clock readings.

    Each element of `ticks` is the local hour on one tick, or a (day, hour)
    pair to cross midnight. The scripted clock is installed in BOTH server and
    rivian_api: set_charge_amps(0) reads the hour itself to pick 06:00 or 18:00.
    """
    from datetime import datetime

    import rivian_api

    def _run(ticks, day=6):
        stamps = [datetime(2026, 9, *(t if isinstance(t, tuple) else (day, t)), 0, 0)
                  for t in ticks]
        readings = iter(stamps)

        class ScriptedDatetime(datetime):
            _current = datetime(2026, 9, day, 0, 0, 0)

            @classmethod
            def now(cls, tz=None):
                return cls._current

        def advance(_seconds):
            try:
                ScriptedDatetime._current = next(readings)
            except StopIteration:
                raise _LoopFinished from None

        monkeypatch.setattr(server_module, 'datetime', ScriptedDatetime)
        monkeypatch.setattr(rivian_api, 'datetime', ScriptedDatetime)
        monkeypatch.setattr(server_module.time, 'sleep', advance)

        with pytest.raises(_LoopFinished):
            server_module._noon_window_shifter()

    return _run


@pytest.fixture
def car_keeps_writes(server_gateway, monkeypatch):
    """Make the fake car return whatever schedule was last written to it."""
    import rivian_api

    def post(url, headers=None, json=None, **kwargs):
        response = server_gateway.post(url, headers=headers, json=json, **kwargs)
        if (json or {}).get('operationName') == 'SetChargingSchedule' \
                and response.status_code == 200:
            server_gateway.set_schedules(json['variables']['chargingSchedules'])
        return response

    monkeypatch.setattr(rivian_api.requests, 'post', post)
    return server_gateway


def _window_at(start_minute):
    return [existing_schedule(8, start_time=start_minute, duration=60)]


# ---------------------------------------------------------------------------
# Where the window goes
# ---------------------------------------------------------------------------

def test_morning_leaves_an_evening_window_where_it_is(run_shifter, server_gateway):
    """Before noon the target is 18:00; a window already there is not rewritten."""
    server_gateway.set_schedules(_window_at(18 * 60))

    run_shifter([6, 8, 10, 11])

    assert server_gateway.calls_for('SetChargingSchedule') == []


def test_shift_runs_once_after_noon(run_shifter, server_gateway):
    """Charging is disabled with the evening window, so the job rewrites it to
    06:00 — where it is already in the past."""
    server_gateway.set_schedules(_window_at(18 * 60))

    run_shifter([11, 12, 13, 14, 15])

    written = server_gateway.schedules_written()
    assert len(written) == 1
    assert written[0][0]['startTime'] == 6 * 60
    assert written[0][0]['duration'] == 60
    assert written[0][0]['amperage'] == 8


def test_midnight_moves_a_morning_window_to_the_evening(run_shifter, server_gateway):
    """CB-3 / CH-011: a window parked at 06:00 during the afternoon must not be
    left there overnight."""
    server_gateway.set_schedules(_window_at(6 * 60))

    run_shifter([(6, 22), (6, 23), (7, 0), (7, 1), (7, 2)])

    written = server_gateway.schedules_written()
    assert len(written) == 1
    assert written[0][0]['startTime'] == 18 * 60
    assert written[0][0]['amperage'] == 8


def test_window_flips_at_every_half_day(run_shifter, car_keeps_writes):
    """Across two full days the window moves once per half-day, always to the
    slot at least 6 h away."""
    car_keeps_writes.set_schedules(_window_at(6 * 60))

    run_shifter([(d, h) for d in (6, 7) for h in range(24)])

    written = [w[0]['startTime'] for w in car_keeps_writes.schedules_written()]
    # 6th 00:00 -> 18:00, 6th 12:00 -> 06:00, 7th 00:00 -> 18:00, 7th 12:00 -> 06:00
    assert written == [18 * 60, 6 * 60, 18 * 60, 6 * 60]


def test_no_shift_while_charging_is_active(run_shifter, server_gateway):
    """A live 24 h schedule must be left alone — the job only moves the window
    when charging is already off."""
    server_gateway.set_schedules([existing_schedule(32)])

    run_shifter([0, 1, 12, 13, 14])

    assert server_gateway.calls_for('SetChargingSchedule') == []


def test_charging_active_is_checked_once_per_half_day(run_shifter, server_gateway):
    """Once a half-day is settled, later ticks in it make no API calls."""
    server_gateway.set_schedules([existing_schedule(32)])

    run_shifter([12, 13, 14, 15])

    reads = server_gateway.calls_for('GetChargingSchedule')
    assert len(reads) == 2      # one tick: get_schedules + get_current_amps


def test_shift_happens_again_the_next_day(run_shifter, server_gateway):
    server_gateway.set_schedules(_window_at(18 * 60))

    run_shifter([12, 13])
    run_shifter([12, 13], day=7)

    assert len(server_gateway.schedules_written()) == 2


# ---------------------------------------------------------------------------
# Failures are retried (CH-004, fixes C-027)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('payload,status', [
    ({'error': 'upstream'}, 500),
    ({'data': None, 'errors': [{'message': 'User is unauthenticated'}]}, 200),
])
def test_a_failed_shift_is_retried_every_tick(run_shifter, server_gateway,
                                              payload, status):
    server_gateway.set_schedules(_window_at(18 * 60))
    server_gateway.set_response('SetChargingSchedule', payload, status_code=status)

    run_shifter([12, 13, 14, 15, 16, 17, 18])

    assert len(server_gateway.calls_for('SetChargingSchedule')) == 7


def test_retrying_stops_once_the_write_lands(run_shifter, car_keeps_writes,
                                             monkeypatch):
    """Two failed writes, then success: exactly three attempts."""
    import rivian_api

    car_keeps_writes.set_schedules(_window_at(18 * 60))
    failures = {'left': 2}
    inner = rivian_api.requests.post

    def flaky(url, headers=None, json=None, **kwargs):
        if (json or {}).get('operationName') == 'SetChargingSchedule' \
                and failures['left']:
            failures['left'] -= 1
            car_keeps_writes.calls.append({'operation': 'SetChargingSchedule',
                                           'variables': json['variables']})
            from conftest import FakeResponse
            return FakeResponse({'error': 'upstream'}, 500)
        return inner(url, headers=headers, json=json, **kwargs)

    monkeypatch.setattr(rivian_api.requests, 'post', flaky)

    run_shifter([12, 13, 14, 15, 16, 17])

    assert len(car_keeps_writes.calls_for('SetChargingSchedule')) == 3
    assert car_keeps_writes.calls_for('GetChargingSchedule')[-1]  # still polled
    final = car_keeps_writes.schedules_written()[-1][0]
    assert final['startTime'] == 6 * 60


def test_an_unreadable_schedule_is_retried_and_never_written(run_shifter,
                                                             server_gateway):
    """A non-200 read looks like "disabled" to get_current_amps(). The shifter
    must not act on it — that would write a window with an empty location
    (E-003) — and must try again next tick."""
    server_gateway.set_response('GetChargingSchedule', '<html>', status_code=503)

    run_shifter([12, 13, 14, 15])

    assert server_gateway.calls_for('SetChargingSchedule') == []
    assert len(server_gateway.calls_for('GetChargingSchedule')) == 4


def test_a_graphql_read_error_is_retried(run_shifter, server_gateway):
    """C-002: data: null makes get_schedules() raise. The half-day stays open."""
    server_gateway.set_response('GetChargingSchedule', {'data': None})

    run_shifter([12, 13, 14, 15])

    assert server_gateway.calls_for('SetChargingSchedule') == []
    assert len(server_gateway.calls_for('GetChargingSchedule')) == 4


# ---------------------------------------------------------------------------
# The shifter holds the schedule lock across read and write (CH-002)
# ---------------------------------------------------------------------------

def test_an_evcc_enable_during_a_shift_is_not_overwritten(run_shifter,
                                                          car_keeps_writes,
                                                          monkeypatch):
    """C-007 / CB-5: EVCC enables charging while the shifter is between its
    read ("charging is off") and its write (re-park the window). The enable
    must wait, then land last, so charging ends up enabled."""
    import _thread
    import threading

    import rivian_api
    import server

    car_keeps_writes.set_schedules(_window_at(18 * 60))
    inner = rivian_api.requests.post
    started = {'done': None}

    def post(url, headers=None, json=None, **kwargs):
        op = (json or {}).get('operationName')
        if op == 'GetChargingSchedule' and started['done'] is None:
            # The shifter's first read: EVCC's enable arrives right now.
            started['done'] = threading.Event()

            def evcc_enable():
                try:
                    server.rivian.enable_charging(True)
                finally:
                    started['done'].set()

            _thread.start_new_thread(evcc_enable, ())
            # Give the enable every chance to run now. Without the lock it
            # completes here, before the shifter's write; with the lock it
            # can't start until the shifter is done.
            started['done'].wait(0.3)
        return inner(url, headers=headers, json=json, **kwargs)

    monkeypatch.setattr(rivian_api.requests, 'post', post)

    run_shifter([12, 13])

    assert started['done'].wait(2), 'the EVCC enable never completed'
    written = car_keeps_writes.schedules_written()
    assert [w[0]['duration'] for w in written] == [60, 1440], \
        'expected the shifter park, then the EVCC enable, in that order'


def test_a_refused_write_is_retried(run_shifter, server_module, server_gateway):
    """E-003: with no location from Rivian and none seen since startup, the
    park is refused (nothing written) and the half-day stays open, so the
    shifter tries again next tick instead of marking it done."""
    server_module.rivian.last_known_location = None
    server_gateway.set_schedules([])

    run_shifter([12, 13, 14])

    assert server_gateway.calls_for('SetChargingSchedule') == []
    # get_schedules + get_current_amps + _apply_schedule's read, every tick
    assert len(server_gateway.calls_for('GetChargingSchedule')) == 9
