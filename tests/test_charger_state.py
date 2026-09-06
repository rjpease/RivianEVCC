"""Baseline: reading charger state back out of the Rivian schedule, plus the
enable/disable path and the IEC 61851 status mapping.
"""

import pytest

import rivian_api
from conftest import existing_schedule

pytestmark = pytest.mark.baseline


# ---------------------------------------------------------------------------
# get_current_amps: a 24-hour enabled schedule means "charging enabled"
# ---------------------------------------------------------------------------

@pytest.mark.charging
@pytest.mark.parametrize('schedule,expected', [
    (existing_schedule(32), 32),                              # 24 h, enabled
    (existing_schedule(8), 8),
    (existing_schedule(48), 48),
    (existing_schedule(32, enabled=False), 0),                # explicitly off
    (existing_schedule(48, start_time=360, duration=60), 0),  # disable window
    (existing_schedule(32, duration=720), 0),                 # not a full day
])
def test_current_amps_reads_the_first_schedule(api, gateway, schedule, expected):
    gateway.set_schedules([schedule])

    assert api.get_current_amps() == expected


@pytest.mark.charging
def test_no_schedules_reads_as_zero(api, gateway):
    gateway.set_schedules([])

    assert api.get_current_amps() == 0


@pytest.mark.charging
def test_only_the_first_schedule_is_consulted(api, gateway):
    """The bridge owns slot 0; anything after it is ignored (finding E-005)."""
    gateway.set_schedules([
        existing_schedule(16),
        existing_schedule(48),
    ])

    assert api.get_current_amps() == 16


# ---------------------------------------------------------------------------
# enable_charging
# ---------------------------------------------------------------------------

@pytest.mark.charging
def test_enable_from_off_restores_the_minimum(api, gateway, at_clock):
    """Enabling writes at least AMPS_MIN so EVCC has something to ramp from;
    EVCC sends its real target immediately afterwards."""
    at_clock(rivian_api, hour=9)
    gateway.set_schedules([existing_schedule(48, start_time=360, duration=60)])

    api.enable_charging(True)

    written = gateway.last_schedule_written()
    assert written['amperage'] == rivian_api.RivianAPI.AMPS_MIN
    assert written['duration'] == 1440


@pytest.mark.charging
def test_enable_preserves_an_already_active_rate(api, gateway, at_clock):
    at_clock(rivian_api, hour=9)
    gateway.set_schedules([existing_schedule(32)])

    api.enable_charging(True)

    # Already a 24 h 32 A schedule, so the rewrite is skipped as redundant.
    assert gateway.calls_for('SetChargingSchedule') == []
    assert api.get_current_amps() == 32


@pytest.mark.charging
@pytest.mark.parametrize('hour,expected_start', [(9, 18 * 60), (14, 6 * 60)])
def test_disable_writes_the_off_window(api, gateway, at_clock, hour,
                                       expected_start):
    at_clock(rivian_api, hour=hour)
    gateway.set_schedules([existing_schedule(32)])

    api.enable_charging(False)

    written = gateway.last_schedule_written()
    assert written['duration'] == 60
    assert written['startTime'] == expected_start


# ---------------------------------------------------------------------------
# IEC 61851 status mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('rivian_status,expected', [
    ('chrgr_sts_connected_charging', 'C'),
    ('chrgr_sts_connected_no_chrg', 'B'),
    ('chrgr_sts_connected_not_charging', 'B'),
    ('chrgr_sts_not_connected', 'A'),
    (None, 'A'),
])
def test_status_mapping(api, rivian_status, expected):
    api.charging_status = rivian_status

    assert api.get_evcc_status() == expected


@pytest.mark.defect
def test_unknown_status_falls_through_to_not_connected(api):
    """Finding E-002: an unmapped *connected* state reads as 'A', which tells
    EVCC the car is unplugged. 'A' is the conservative default, so this is
    pinned as-is — the mapping should gain values, not change its fallback."""
    api.charging_status = 'chrgr_sts_connected_charging_complete'

    assert api.get_evcc_status() == 'A'


def test_is_charging_tracks_the_charging_status(api):
    api.charging_status = 'chrgr_sts_connected_charging'
    assert api.is_charging() is True

    api.charging_status = 'chrgr_sts_connected_no_chrg'
    assert api.is_charging() is False
