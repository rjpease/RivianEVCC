"""Baseline: the HTTP contract EVCC consumes.

EVCC's custom vehicle and charger plugins parse these responses directly, so
the exact status code, body text and method matter. Anything asserted here is
part of the integration contract, not an implementation detail.
"""

import pytest

from conftest import existing_schedule

pytestmark = pytest.mark.baseline


# ---------------------------------------------------------------------------
# Vehicle plugin
# ---------------------------------------------------------------------------

def test_soc_returns_a_rounded_integer(client, server_module):
    server_module.rivian.battery_level = 72.4

    response = client.get('/soc')

    assert response.status_code == 200
    assert response.get_data(as_text=True) == '72'


def test_soc_is_unavailable_before_the_first_refresh(client, server_module):
    server_module.rivian.battery_level = None

    response = client.get('/soc')

    assert response.status_code == 503


def test_range_returns_an_integer(client, server_module):
    server_module.rivian.distance_to_empty_km = 402

    response = client.get('/range')

    assert response.status_code == 200
    assert response.get_data(as_text=True) == '402'


def test_range_is_unavailable_when_unknown(client, server_module):
    server_module.rivian.distance_to_empty_km = None

    assert client.get('/range').status_code == 503


@pytest.mark.parametrize('rivian_status,expected', [
    ('chrgr_sts_connected_charging', 'C'),
    ('chrgr_sts_connected_no_chrg', 'B'),
    ('chrgr_sts_not_connected', 'A'),
])
def test_status_endpoints_agree(client, server_module, rivian_status, expected):
    """/status and /charger/status are the same value by design."""
    server_module.rivian.charging_status = rivian_status

    assert client.get('/status').get_data(as_text=True) == expected
    assert client.get('/charger/status').get_data(as_text=True) == expected


def test_status_endpoints_never_hit_the_api(client, server_module,
                                            server_gateway):
    """Vehicle reads are served from cache; only charger reads go upstream."""
    server_module.rivian.charging_status = 'chrgr_sts_connected_charging'

    client.get('/soc')
    client.get('/status')
    client.get('/range')
    client.get('/charger/status')

    assert server_gateway.calls == []


# ---------------------------------------------------------------------------
# Charger plugin: reads
# ---------------------------------------------------------------------------

@pytest.mark.charging
@pytest.mark.parametrize('schedule,expected', [
    (existing_schedule(32), 'true'),
    (existing_schedule(8), 'true'),
    (existing_schedule(48, start_time=360, duration=60), 'false'),
    (existing_schedule(32, enabled=False), 'false'),
])
def test_charger_enabled_reports_the_schedule(client, server_gateway, schedule,
                                              expected):
    server_gateway.set_schedules([schedule])

    response = client.get('/charger/enabled')

    assert response.status_code == 200
    assert response.get_data(as_text=True) == expected


@pytest.mark.charging
def test_charger_current_reports_the_amperage(client, server_gateway):
    server_gateway.set_schedules([existing_schedule(24)])

    response = client.get('/charger/current')

    assert response.status_code == 200
    assert response.get_data(as_text=True) == '24'


# ---------------------------------------------------------------------------
# Charger plugin: writes
# ---------------------------------------------------------------------------

@pytest.mark.charging
def test_set_current_accepts_evccs_plain_integer_body(client, server_gateway):
    response = client.post('/charger/current', data='16')

    assert response.status_code == 204
    assert server_gateway.last_schedule_written()['amperage'] == 16


@pytest.mark.charging
@pytest.mark.parametrize('body,expected', [
    ('16', 16), (' 32 ', 32), ('48', 48), ('16.0', 16),
])
def test_set_current_body_parsing(client, server_gateway, body, expected):
    client.post('/charger/current', data=body)

    assert server_gateway.last_schedule_written()['amperage'] == expected


@pytest.mark.charging
def test_set_current_zero_disables(client, server_gateway):
    response = client.post('/charger/current', data='0')

    assert response.status_code == 204
    assert server_gateway.last_schedule_written()['duration'] == 60


@pytest.mark.charging
@pytest.mark.parametrize('body', ['', 'abc', 'sixteen', 'null'])
def test_unparseable_current_is_rejected(client, server_gateway, body):
    response = client.post('/charger/current', data=body)

    assert response.status_code == 400
    assert server_gateway.calls_for('SetChargingSchedule') == []


@pytest.mark.charging
def test_enable_accepts_evccs_boolean_body(client, server_gateway):
    server_gateway.set_schedules([existing_schedule(48, start_time=360,
                                                    duration=60)])

    response = client.post('/charger/enable', data='true')

    assert response.status_code == 204
    assert server_gateway.last_schedule_written()['duration'] == 1440


@pytest.mark.charging
def test_disable_writes_the_off_window(client, server_gateway):
    server_gateway.set_schedules([existing_schedule(32)])

    response = client.post('/charger/enable', data='false')

    assert response.status_code == 204
    assert server_gateway.last_schedule_written()['duration'] == 60


@pytest.mark.charging
def test_enable_is_case_insensitive(client, server_gateway):
    server_gateway.set_schedules([existing_schedule(48, start_time=360,
                                                    duration=60)])

    client.post('/charger/enable', data='TRUE')

    assert server_gateway.last_schedule_written()['duration'] == 1440


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_health_reports_the_cached_vehicle_state(client, server_module,
                                                 server_gateway):
    server_module.rivian.battery_level = 72.4
    server_module.rivian.charging_status = 'chrgr_sts_connected_charging'
    server_module.rivian.distance_to_empty_km = 402
    server_gateway.set_schedules([existing_schedule(32)])

    body = client.get('/health').get_json()

    assert body['soc'] == 72.4
    assert body['evcc_status'] == 'C'
    assert body['range_km'] == 402
    assert body['rivian_charging_status'] == 'chrgr_sts_connected_charging'
    assert body['charger_enabled'] is True
    assert body['charger_current_amps'] == 32
    assert isinstance(body['last_refresh_ago_seconds'], int)


# ---------------------------------------------------------------------------
# Method contract
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('path', [
    '/soc', '/status', '/range',
    '/charger/status', '/charger/enabled', '/charger/current', '/health',
])
def test_read_endpoints_are_get(client, server_gateway, path):
    server_gateway.set_schedules([existing_schedule(16)])

    assert client.get(path).status_code in (200, 503)


def test_enable_is_post_only(client):
    assert client.get('/charger/enable').status_code == 405


def test_current_serves_both_methods(client, server_gateway):
    """POST sets the current; GET reads it back. EVCC only uses POST — the
    getter exists for debugging (finding C-012)."""
    server_gateway.set_schedules([existing_schedule(16)])

    assert client.get('/charger/current').status_code == 200
    assert client.post('/charger/current', data='16').status_code == 204
