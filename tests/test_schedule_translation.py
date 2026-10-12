"""Baseline: EVCC amperage -> Rivian charging schedule.

This is the most important file in the suite. Every assertion here pins
CURRENT, WORKING charging behaviour so that later refactoring cannot move it
silently. If a test in this file fails, charging behaviour has changed.

Do not "fix" an expectation here to make a change pass. The charging algorithm
is the specification; a failure means the change is wrong, or that the change
is a deliberate charging-logic change that needs explicit sign-off.
"""

import pytest

import rivian_api
from conftest import ALL_WEEKDAYS, existing_schedule

pytestmark = [pytest.mark.baseline, pytest.mark.charging]


# ---------------------------------------------------------------------------
# Charging: a 24-hour all-day schedule at the requested amperage
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('requested', [8, 9, 12, 16, 24, 32, 40, 48])
def test_amps_in_range_write_a_full_day_schedule(api, gateway, at_clock, requested):
    at_clock(rivian_api, hour=9)

    api.set_charge_amps(requested)

    written = gateway.last_schedule_written()
    assert written['amperage'] == requested
    assert written['startTime'] == 0
    assert written['duration'] == 1440      # the "charging enabled" marker
    assert written['enabled'] is True
    assert written['weekDays'] == ALL_WEEKDAYS


@pytest.mark.parametrize('requested,expected', [
    (1, 8), (2, 8), (5, 8), (7, 8),         # below minimum -> clamped up
    (49, 48), (60, 48), (1000, 48),          # above maximum -> clamped down
])
def test_out_of_range_amps_are_clamped(api, gateway, at_clock, requested, expected):
    """8 A floor and 48 A ceiling. The floor is intentional: EVCC's mincurrent
    is 8 A and the vehicle cannot charge slower."""
    at_clock(rivian_api, hour=9)

    api.set_charge_amps(requested)

    assert gateway.last_schedule_written()['amperage'] == expected


def test_float_and_string_amps_are_coerced(api, gateway, at_clock):
    at_clock(rivian_api, hour=9)

    api.set_charge_amps(16.9)
    assert gateway.last_schedule_written()['amperage'] == 16

    api.set_charge_amps('32')
    assert gateway.last_schedule_written()['amperage'] == 32


# ---------------------------------------------------------------------------
# Not charging: a one-hour window parked outside the current time
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('hour,expected_start', [
    (0, 18 * 60), (6, 18 * 60), (9, 18 * 60), (11, 18 * 60),   # before noon
    (12, 6 * 60), (13, 6 * 60), (18, 6 * 60), (23, 6 * 60),    # noon onward
])
def test_zero_amps_parks_a_one_hour_window(api, gateway, at_clock, hour,
                                           expected_start):
    """0 A disables charging by writing a 60-minute window that does not cover
    'now': 18:00 in the morning, 06:00 from noon onward.

    The window is parked at 8 A, not 48 A (CH-008, approved 2026-10-12), so a
    window that is somehow never moved charges at the minimum rate."""
    at_clock(rivian_api, hour=hour)

    assert api.set_charge_amps(0) is True

    written = gateway.last_schedule_written()
    assert written['startTime'] == expected_start
    assert written['duration'] == 60
    assert written['amperage'] == 8
    assert written['amperage'] == rivian_api.RivianAPI.AMPS_PARK
    assert written['enabled'] is True
    assert written['weekDays'] == ALL_WEEKDAYS


def test_noon_is_the_pivot_exactly(api, gateway, at_clock):
    """11:59 still writes the evening window; 12:00 writes the morning one."""
    at_clock(rivian_api, hour=11, minute=59)
    api.set_charge_amps(0)
    assert gateway.last_schedule_written()['startTime'] == 18 * 60

    at_clock(rivian_api, hour=12, minute=0)
    api.set_charge_amps(0)
    assert gateway.last_schedule_written()['startTime'] == 6 * 60


def test_zero_is_the_only_value_that_disables(api, gateway, at_clock):
    at_clock(rivian_api, hour=9)

    api.set_charge_amps(0)
    assert gateway.last_schedule_written()['duration'] == 60

    api.set_charge_amps(1)
    assert gateway.last_schedule_written()['duration'] == 1440


# ---------------------------------------------------------------------------
# Location is carried over from the vehicle's existing schedule
# ---------------------------------------------------------------------------

def test_location_is_preserved_from_the_existing_schedule(api, gateway, at_clock):
    at_clock(rivian_api, hour=9)
    home = {'latitude': 37.7749, 'longitude': -122.4194}
    gateway.set_schedules([existing_schedule(16, location=home)])

    api.set_charge_amps(32)

    assert gateway.last_schedule_written()['location'] == home


def test_location_is_empty_when_the_vehicle_has_no_schedule(api, gateway, at_clock):
    at_clock(rivian_api, hour=9)
    gateway.set_schedules([])

    api.set_charge_amps(32)

    assert gateway.last_schedule_written()['location'] == {}


# ---------------------------------------------------------------------------
# Redundant writes are skipped
# ---------------------------------------------------------------------------

def test_identical_schedule_is_not_rewritten(api, gateway, at_clock):
    at_clock(rivian_api, hour=9)
    gateway.set_schedules([existing_schedule(16)])

    api.set_charge_amps(16)

    assert gateway.calls_for('SetChargingSchedule') == []


def test_a_different_amperage_is_written(api, gateway, at_clock):
    at_clock(rivian_api, hour=9)
    gateway.set_schedules([existing_schedule(16)])

    api.set_charge_amps(24)

    assert gateway.last_schedule_written()['amperage'] == 24


# ---------------------------------------------------------------------------
# Sequences EVCC actually produces
# ---------------------------------------------------------------------------

def test_solar_ramp_writes_each_step(api, gateway, at_clock):
    """EVCC ramping on solar surplus: every distinct target reaches the car."""
    at_clock(rivian_api, hour=9)

    for amps in (8, 16, 32, 48, 32, 16, 8):
        api.set_charge_amps(amps)

    assert [s['amperage'] for s in
            (w[0] for w in gateway.schedules_written())] == [8, 16, 32, 48, 32, 16, 8]


def test_rapid_repeated_writes_leave_the_last_value(api, gateway, at_clock):
    at_clock(rivian_api, hour=9)

    for amps in (16, 16, 16, 24):
        api.set_charge_amps(amps)

    assert gateway.last_schedule_written()['amperage'] == 24


def test_disable_then_enable_round_trip(api, gateway, at_clock):
    at_clock(rivian_api, hour=14)

    api.set_charge_amps(0)
    assert gateway.last_schedule_written()['duration'] == 60

    api.set_charge_amps(24)
    written = gateway.last_schedule_written()
    assert written['duration'] == 1440
    assert written['amperage'] == 24


# ---------------------------------------------------------------------------
# The mutation itself
# ---------------------------------------------------------------------------

def test_write_targets_the_right_vehicle_and_replaces_the_list(api, gateway,
                                                               at_clock):
    """One schedule replaces the whole list — see finding E-005."""
    at_clock(rivian_api, hour=9)

    api.set_charge_amps(16)

    call = gateway.calls_for('SetChargingSchedule')[-1]
    assert call['variables']['vehicleId'] == 'vehicle-id-stub'
    assert len(call['variables']['chargingSchedules']) == 1


def test_write_sends_the_session_headers(api, gateway, at_clock):
    at_clock(rivian_api, hour=9)

    api.set_charge_amps(16)

    headers = gateway.calls_for('SetChargingSchedule')[-1]['headers']
    assert headers['a-sess'] == 'stored-app'
    assert headers['u-sess'] == 'stored-user'
    assert headers['csrf-token'] == 'stored-csrf'


# ---------------------------------------------------------------------------
# Did the park write land? (needed so the window shifter can retry, CH-004)
# ---------------------------------------------------------------------------

def test_park_reports_success(api, gateway, at_clock):
    at_clock(rivian_api, hour=13)

    assert api.set_charge_amps(0) is True


def test_park_already_in_place_counts_as_success(api, gateway, at_clock):
    """The read-compare skip writes nothing, and the car already has the
    window, so the caller must treat it as done."""
    at_clock(rivian_api, hour=13)
    gateway.set_schedules([existing_schedule(8, start_time=6 * 60, duration=60)])

    assert api.set_charge_amps(0) is True
    assert gateway.calls_for('SetChargingSchedule') == []


@pytest.mark.parametrize('payload,status', [
    ({'error': 'upstream'}, 500),
    ({'data': None, 'errors': [{'message': 'User is unauthenticated'}]}, 200),
    ({'data': {'setChargingSchedules': {'success': False}}}, 200),
    ('<html>rate limited</html>', 200),
])
def test_park_reports_failure(api, gateway, at_clock, payload, status):
    """A non-200, a GraphQL error carried in a 200 (C-002), an explicit
    success: false, or a non-JSON body all mean the window was not written."""
    at_clock(rivian_api, hour=13)
    gateway.set_response('SetChargingSchedule', payload, status_code=status)

    assert api.set_charge_amps(0) is False
