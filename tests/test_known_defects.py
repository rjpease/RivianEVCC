"""Pins CURRENT behaviour at each known defect.

These tests assert what the code does today, not what it should do. Each names
the finding it corresponds to. When that finding is fixed, the test is meant to
fail: update it deliberately, as part of the fix, so no defect is repaired by
accident and none is reintroduced silently.

Findings marked CHARGING LOGIC require sign-off before their behaviour changes.
"""

import pytest

import rivian_api
from conftest import existing_schedule

pytestmark = pytest.mark.defect


# ---------------------------------------------------------------------------
# C-003 / CB-1 — a failed write is reported to EVCC as success  [CHARGING LOGIC]
# ---------------------------------------------------------------------------

@pytest.mark.charging
def test_http_500_on_write_is_reported_as_success(client, server_gateway):
    """C-003: the mutation fails upstream and EVCC still gets 204.

    Fixed by CH-001 (return 502). EVCC's maxcurrent is write-only, so it never
    re-reads the value — a lost write diverges permanently.
    """
    server_gateway.set_response('SetChargingSchedule',
                                {'error': 'upstream'}, status_code=500)

    response = client.post('/charger/current', data='16')

    assert response.status_code == 204


@pytest.mark.charging
def test_graphql_errors_on_write_are_reported_as_success(client, server_gateway):
    """C-002 + C-003: GraphQL returns 200 with an errors array; only the status
    code is checked, so this is logged as 'updated successfully'."""
    server_gateway.set_response('SetChargingSchedule', {
        'data': None,
        'errors': [{'message': 'UNAUTHENTICATED', 'extensions': {'code': 401}}],
    })

    assert client.post('/charger/current', data='16').status_code == 204


@pytest.mark.charging
def test_success_false_is_reported_as_success(client, server_gateway):
    """C-003: the mutation selects `{ success }` and then discards it."""
    server_gateway.set_response('SetChargingSchedule',
                                {'data': {'setChargingSchedules':
                                          {'success': False}}})

    assert client.post('/charger/current', data='16').status_code == 204


@pytest.mark.charging
def test_disable_failure_is_reported_as_success(client, server_gateway):
    server_gateway.set_response('SetChargingSchedule',
                                {'error': 'upstream'}, status_code=503)

    assert client.post('/charger/enable', data='false').status_code == 204


# ---------------------------------------------------------------------------
# C-008 — an outage is indistinguishable from "charging disabled"
# ---------------------------------------------------------------------------

@pytest.mark.charging
def test_read_failure_reports_charging_as_disabled(client, server_gateway):
    """C-008: get_schedules() returns None on any non-200 and get_current_amps()
    turns that into 0 — the same answer as genuinely-off. Fixed by CH-006."""
    server_gateway.set_response('GetChargingSchedule',
                                {'error': 'upstream'}, status_code=502)

    enabled = client.get('/charger/enabled')
    current = client.get('/charger/current')

    assert enabled.status_code == 200
    assert enabled.get_data(as_text=True) == 'false'
    assert current.status_code == 200
    assert current.get_data(as_text=True) == '0'


@pytest.mark.charging
def test_enable_during_an_outage_writes_the_minimum(api, gateway, at_clock):
    """C-008 follow-on: enable() reads 0 from a failed query and writes 8 A —
    a charging command provoked by an outage rather than by EVCC."""
    at_clock(rivian_api, hour=9)
    gateway.set_response('GetChargingSchedule',
                         {'error': 'upstream'}, status_code=502)

    api.enable_charging(True)

    assert gateway.last_schedule_written()['amperage'] == 8


# ---------------------------------------------------------------------------
# C-002 — GraphQL errors arrive with HTTP 200 and crash the parser
# ---------------------------------------------------------------------------

def test_null_data_on_a_schedule_read_raises_typeerror(api, gateway):
    """C-002: `data: null` makes get_schedules() subscript None. Only KeyError
    and IndexError are handled anywhere, so this escapes as a TypeError."""
    gateway.set_response('GetChargingSchedule',
                         {'data': None, 'errors': [{'message': 'UNAUTHENTICATED'}]})

    with pytest.raises(TypeError):
        api.get_current_amps()


def test_null_data_escapes_the_request_handler(client, server_gateway):
    """C-002: the TypeError propagates out of the view. Under TESTING the test
    client re-raises it; in production Werkzeug turns it into a 500, so EVCC
    sees the charger error out on its polling path."""
    server_gateway.set_response('GetChargingSchedule', {'data': None})

    with pytest.raises(TypeError):
        client.get('/charger/enabled')


# ---------------------------------------------------------------------------
# C-018 — OverflowError escapes the current-parsing guard
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('body', ['inf', '1e400', '-inf'])
def test_infinite_current_raises_instead_of_400(client, body):
    """C-018: `except ValueError` does not cover int(float('inf'))."""
    with pytest.raises(OverflowError):
        client.post('/charger/current', data=body)


def test_nan_current_is_rejected_correctly(client):
    """int(float('nan')) does raise ValueError, so NaN is handled."""
    assert client.post('/charger/current', data='nan').status_code == 400


# ---------------------------------------------------------------------------
# E-009 / CB-4 — negative current starts charging  [CHARGING LOGIC]
# ---------------------------------------------------------------------------

@pytest.mark.charging
@pytest.mark.parametrize('body,expected', [('-1', 8), ('-5', 8), ('-48', 8)])
def test_negative_current_starts_charging_at_the_minimum(client, server_gateway,
                                                         body, expected):
    """E-009: only exactly 0 disables. A negative value falls through to the
    clamp and becomes an 8 A charging schedule. Fixed by CH-005 (reject with
    400) — the 1-7 A -> 8 A clamp is intentional and must not change."""
    response = client.post('/charger/current', data=body)

    assert response.status_code == 204
    written = server_gateway.last_schedule_written()
    assert written['amperage'] == expected
    assert written['duration'] == 1440


# ---------------------------------------------------------------------------
# E-008 — any body that is not "true" disables  [CHARGING LOGIC]
# ---------------------------------------------------------------------------

@pytest.mark.charging
@pytest.mark.parametrize('body', ['', 'yes', '1', 'on', 'enabled', 'garbage'])
def test_non_true_enable_bodies_disable_charging(client, server_gateway, body):
    """E-008: an empty or malformed body is treated as disable. Fixed by CH-005.
    EVCC always sends 'true'/'false', so this is only reachable from a stray
    request — which finding S-001 makes possible from anywhere on the LAN."""
    server_gateway.set_schedules([existing_schedule(32)])

    client.post('/charger/enable', data=body)

    assert server_gateway.last_schedule_written()['duration'] == 60


# ---------------------------------------------------------------------------
# E-004 — schedule fields are read without type or null validation
# ---------------------------------------------------------------------------

def test_null_amperage_is_served_verbatim_to_evcc(client, server_gateway):
    """E-004: a present-but-null amperage becomes the literal string 'None',
    which EVCC cannot parse as an integer."""
    server_gateway.set_schedules([existing_schedule(None)])

    assert client.get('/charger/current').get_data(as_text=True) == 'None'


def test_null_amperage_breaks_the_enabled_endpoint(client, server_gateway):
    """E-004: `None >= 8` raises TypeError on EVCC's polling path, which
    production serves as a 500."""
    server_gateway.set_schedules([existing_schedule(None)])

    with pytest.raises(TypeError):
        client.get('/charger/enabled')


# ---------------------------------------------------------------------------
# C-010 — /health cannot report unhealthy, and costs two API calls
# ---------------------------------------------------------------------------

def test_health_says_ok_even_when_the_api_is_down(client, server_module,
                                                  server_gateway):
    """C-010: 'status' is a hard-coded literal."""
    server_module.rivian.charging_status = None
    server_module.rivian.battery_level = None
    server_gateway.set_schedules([])

    response = client.get('/health')

    assert response.status_code == 200
    assert response.get_json()['status'] == 'ok'


def test_health_queries_the_schedule_twice(client, server_gateway):
    """C-010: get_current_amps() is called once for `charger_enabled` and again
    for `charger_current_amps`."""
    server_gateway.set_schedules([existing_schedule(16)])

    client.get('/health')

    assert len(server_gateway.calls_for('GetChargingSchedule')) == 2


# ---------------------------------------------------------------------------
# C-019 — redundant calls on the write path
# ---------------------------------------------------------------------------

def test_enable_costs_three_api_calls(client, server_gateway):
    """C-019: read amps -> read schedule -> mutate."""
    server_gateway.set_schedules([existing_schedule(48, start_time=360,
                                                    duration=60)])

    client.post('/charger/enable', data='true')

    assert server_gateway.operations() == [
        'GetChargingSchedule',    # get_current_amps()
        'GetChargingSchedule',    # _apply_schedule()'s own read
        'SetChargingSchedule',
    ]


def test_every_enabled_poll_hits_the_api(client, server_gateway):
    """C-019: EVCC polls this every cycle; each poll is a live GraphQL query."""
    server_gateway.set_schedules([existing_schedule(16)])

    for _ in range(5):
        client.get('/charger/enabled')

    assert len(server_gateway.calls_for('GetChargingSchedule')) == 5


# ---------------------------------------------------------------------------
# C-004 — no request timeout is ever passed
# ---------------------------------------------------------------------------

def test_no_request_carries_a_timeout(api, gateway, at_clock):
    """C-004: requests.post() is called without `timeout=`, so a half-open
    socket blocks the calling thread forever."""
    at_clock(rivian_api, hour=9)
    api.refresh()
    api.set_charge_amps(16)

    assert gateway.calls, 'expected outbound calls to inspect'
    assert all('timeout' not in call['kwargs'] for call in gateway.calls)


# ---------------------------------------------------------------------------
# C-009 — last_refresh records the attempt, not the success
# ---------------------------------------------------------------------------

def test_failed_refresh_still_advances_the_freshness_clock(server_module,
                                                           server_gateway):
    """C-009: _background_refresher sets _last_refresh after any refresh() that
    returns, and refresh() returns normally on failure — so /health reports a
    healthy age over a cache that never updated."""
    server_module.rivian.battery_level = 41.0        # last known-good reading
    server_gateway.set_response('GetVehicleState',
                                {'error': 'upstream'}, status_code=500)
    before = server_module._last_refresh

    # What _background_refresher does on every tick: refresh, then stamp.
    server_module.rivian.refresh()
    server_module._last_refresh = server_module.time.time()

    assert server_module._last_refresh >= before     # looks freshly refreshed
    assert server_module.rivian.battery_level == 41.0  # but nothing was learned
