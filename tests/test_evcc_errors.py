"""Baseline: EVCC is told when Rivian fails (CH-001, CH-006).

CH-001: a schedule write that did not land returns 502 instead of 204, so
EVCC logs it and retries next cycle. Before, EVCC was told "done" and — because
maxcurrent is write-only — never found out (C-003 / CB-1).

CH-006: a schedule read that failed returns 502 instead of "false" / "0", so
EVCC waits instead of treating an outage as "charging is off" (C-008). A
malformed schedule (null amperage, E-004) is treated the same way.

A false alarm heals itself: if a write landed but its response couldn't be
confirmed, EVCC's retry finds the schedule already in place and gets 204.
"""

import pytest

from conftest import existing_schedule

pytestmark = [pytest.mark.baseline, pytest.mark.charging]

WRITE_FAILURES = [
    pytest.param({'error': 'upstream'}, 500, id='http-500'),
    pytest.param({'data': None, 'errors': [{'message': 'UNAUTHENTICATED'}]}, 200,
                 id='graphql-error-in-200'),
    pytest.param({'data': {'setChargingSchedules': {'success': False}}}, 200,
                 id='success-false'),
    pytest.param('<html>rate limited</html>', 200, id='html-body'),
]

READ_FAILURES = [
    pytest.param({'error': 'upstream'}, 500, id='http-500'),
    pytest.param('<html>rate limited</html>', 503, id='html-503'),
    pytest.param({'data': None}, 200, id='data-null'),
]


# ---------------------------------------------------------------------------
# CH-001 — writes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('payload,status', WRITE_FAILURES)
@pytest.mark.parametrize('path,body', [
    ('/charger/current', '16'),
    ('/charger/enable', 'true'),
    ('/charger/enable', 'false'),
])
def test_failed_write_returns_502(client, server_gateway, path, body,
                                  payload, status):
    server_gateway.set_response('SetChargingSchedule', payload, status_code=status)

    response = client.post(path, data=body)

    assert response.status_code == 502
    assert 'not written' in response.get_json()['error']


@pytest.mark.parametrize('path,body', [
    ('/charger/current', '16'),
    ('/charger/enable', 'true'),
    ('/charger/enable', 'false'),
])
def test_successful_write_returns_204(client, server_gateway, path, body):
    assert client.post(path, data=body).status_code == 204
    assert server_gateway.calls_for('SetChargingSchedule')


def test_unchanged_schedule_returns_204_without_a_write(client, server_gateway):
    server_gateway.set_schedules([existing_schedule(16)])

    assert client.post('/charger/current', data='16').status_code == 204
    assert server_gateway.calls_for('SetChargingSchedule') == []


def test_refused_write_without_a_location_returns_502(client, server_module,
                                                      server_gateway):
    """E-003: no location from Rivian and none seen — nothing is written."""
    server_module.rivian.last_known_location = None
    server_gateway.set_schedules([])

    assert client.post('/charger/enable', data='false').status_code == 502
    assert server_gateway.calls_for('SetChargingSchedule') == []


def test_a_false_alarm_heals_on_evccs_retry(client, server_gateway):
    """The write landed but the response couldn't be confirmed: 502 once,
    then EVCC's retry finds the schedule in place and gets 204."""
    server_gateway.set_response('SetChargingSchedule', '<html>gateway timeout</html>')

    assert client.post('/charger/current', data='16').status_code == 502

    # The car did take it (the bridge reads back what it holds).
    server_gateway.set_schedules([existing_schedule(16)])
    assert client.post('/charger/current', data='16').status_code == 204


def test_stale_data_still_returns_503_first(client, server_module, server_gateway):
    server_module._last_refresh = 0.0

    assert client.post('/charger/current', data='16').status_code == 503
    assert server_gateway.calls == []


# ---------------------------------------------------------------------------
# CH-006 — reads
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('payload,status', READ_FAILURES)
@pytest.mark.parametrize('path', ['/charger/enabled', '/charger/current'])
def test_failed_read_returns_502(client, server_gateway, path, payload, status):
    server_gateway.set_response('GetChargingSchedule', payload, status_code=status)

    response = client.get(path)

    assert response.status_code == 502
    assert 'could not read' in response.get_json()['error']


@pytest.mark.parametrize('path', ['/charger/enabled', '/charger/current'])
def test_null_amperage_returns_502(client, server_gateway, path):
    """E-004: a malformed schedule is "unknown", not "None" or a 500."""
    server_gateway.set_schedules([existing_schedule(None)])

    assert client.get(path).status_code == 502


@pytest.mark.parametrize('schedules,enabled,current', [
    ([existing_schedule(32)], 'true', '32'),
    ([existing_schedule(8, start_time=360, duration=60)], 'false', '0'),
    ([], 'false', '0'),
])
def test_good_reads_are_unchanged(client, server_gateway, schedules, enabled,
                                  current):
    server_gateway.set_schedules(schedules)

    assert client.get('/charger/enabled').get_data(as_text=True) == enabled
    assert client.get('/charger/current').get_data(as_text=True) == current
