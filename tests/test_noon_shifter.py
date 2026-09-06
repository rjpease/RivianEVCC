"""Baseline: the background job that moves the disable window past noon.

`_noon_window_shifter` is an infinite loop, so these tests drive it with a
patched `time.sleep` that advances a scripted clock and stops the loop after a
fixed number of ticks.

The job exists to stop an 18:00 disable window (written during the morning)
from arriving that evening as a real 48 A charging hour. Its correctness is
charging behaviour.
"""

import pytest

from conftest import existing_schedule

pytestmark = [pytest.mark.charging, pytest.mark.baseline]


class _LoopFinished(Exception):
    """Breaks out of the shifter's `while True`."""


@pytest.fixture
def run_shifter(server_module, monkeypatch):
    """Run `_noon_window_shifter` over a scripted sequence of clock readings.

    Each element of `hours` is the local hour reported on one tick.
    """
    from datetime import datetime

    def _run(hours, day=6):
        readings = iter([datetime(2026, 9, day, hour, 0, 0) for hour in hours])

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
        monkeypatch.setattr(server_module.time, 'sleep', advance)

        with pytest.raises(_LoopFinished):
            server_module._noon_window_shifter()

    return _run


def test_no_shift_before_noon(run_shifter, server_gateway):
    server_gateway.set_schedules([existing_schedule(48, start_time=1080,
                                                    duration=60)])

    run_shifter([6, 8, 10, 11])

    assert server_gateway.calls == []


def test_shift_runs_once_after_noon(run_shifter, server_gateway):
    """Charging is disabled with the evening window, so the job rewrites it to
    06:00 — where it is already in the past."""
    server_gateway.set_schedules([existing_schedule(48, start_time=1080,
                                                    duration=60)])

    run_shifter([11, 12, 13, 14, 15])

    written = server_gateway.schedules_written()
    assert len(written) == 1
    assert written[0][0]['startTime'] == 6 * 60
    assert written[0][0]['duration'] == 60


def test_no_shift_while_charging_is_active(run_shifter, server_gateway):
    """A live 24 h schedule must be left alone — the job only moves the window
    when charging is already off."""
    server_gateway.set_schedules([existing_schedule(32)])

    run_shifter([12, 13, 14])

    assert server_gateway.calls_for('SetChargingSchedule') == []


def test_shift_happens_again_the_next_day(run_shifter, server_gateway):
    server_gateway.set_schedules([existing_schedule(48, start_time=1080,
                                                    duration=60)])

    run_shifter([12, 13])
    run_shifter([12, 13], day=7)

    assert len(server_gateway.schedules_written()) == 2


@pytest.mark.defect
def test_a_failed_shift_is_not_retried_that_day(run_shifter, server_gateway):
    """Finding C-027 / CB-2.

    `last_shift_date` is assigned BEFORE the try block, so one transient
    failure marks the day done. The 18:00 window then survives into the
    evening — exactly the unwanted charge this job was added to prevent.

    Fixed by CH-004 (set the date only after success). When that lands, this
    test should assert a retry instead.
    """
    server_gateway.set_schedules([existing_schedule(48, start_time=1080,
                                                    duration=60)])
    server_gateway.set_response('SetChargingSchedule',
                                {'error': 'upstream'}, status_code=500)

    run_shifter([12, 13, 14, 15, 16, 17, 18])

    # One attempt, at the first post-noon tick, and no retry despite six more
    # opportunities before the 18:00 window arrives.
    assert len(server_gateway.calls_for('SetChargingSchedule')) == 1


@pytest.mark.defect
def test_a_read_failure_also_burns_the_day(run_shifter, server_gateway):
    """C-027 again, via the read half: get_current_amps() raising means the
    day is marked shifted without anything having been written."""
    server_gateway.set_response('GetChargingSchedule', {'data': None})

    run_shifter([12, 13, 14, 15])

    assert server_gateway.calls_for('SetChargingSchedule') == []
    assert len(server_gateway.calls_for('GetChargingSchedule')) == 1
