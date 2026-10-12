"""Baseline: every charging-schedule read and write is serialised (CH-002).

Without the lock, EVCC's request handlers and the window shifter could
interleave between a schedule read and the write that depends on it, so a
shifter re-park could overwrite an EVCC enable (C-007 / CB-5).

Threads are started with `_thread` because the server fixture replaces
`threading.Thread` with a dummy so that importing server starts no daemons.
"""

import _thread
import threading
import time

import pytest

import rivian_api

pytestmark = [pytest.mark.baseline, pytest.mark.charging]


def run_in_thread(fn, *args):
    """Start fn(*args) on a real thread; return an Event set when it finishes."""
    done = threading.Event()

    def target():
        try:
            fn(*args)
        finally:
            done.set()

    _thread.start_new_thread(target, ())
    return done


@pytest.mark.parametrize('call', [
    lambda api: api.set_charge_amps(16),
    lambda api: api.set_charge_amps(0),
    lambda api: api.enable_charging(True),
    lambda api: api.enable_charging(False),
    lambda api: api.get_current_amps(),
])
def test_schedule_access_waits_for_the_lock(api, gateway, at_clock, call):
    at_clock(rivian_api, hour=9)

    with api.schedule_lock:
        done = run_in_thread(call, api)
        time.sleep(0.2)
        assert gateway.calls == [], 'schedule API was called while the lock was held'

    assert done.wait(2), 'call never completed after the lock was released'
    assert gateway.calls, 'call made no schedule request at all'


def test_nested_calls_do_not_deadlock(api, gateway, at_clock):
    """enable_charging -> set_charge_amps -> _apply_schedule all take the lock."""
    at_clock(rivian_api, hour=9)

    done = run_in_thread(api.enable_charging, True)

    assert done.wait(2), 'enable_charging deadlocked on its own lock'
    assert gateway.last_schedule_written()['duration'] == 1440
