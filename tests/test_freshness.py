"""Baseline: only a successful refresh counts as fresh data (C-009).

Before the fix, refresh() returned normally on failure and the background
refresher stamped _last_refresh anyway, so a dead Rivian session (every call
UNAUTHENTICATED) never looked stale: /health said "stale": false, the 503
guard never tripped, and Home Assistant's stale alert could never fire.
"""

import pytest

pytestmark = pytest.mark.baseline

UNAUTHENTICATED = {'data': None, 'errors': [
    {'extensions': {'code': 'UNAUTHENTICATED'}, 'message': 'User is unauthenticated'}]}


# ---------------------------------------------------------------------------
# refresh() reports whether it learned anything
# ---------------------------------------------------------------------------

def test_refresh_reports_success(api, gateway):
    assert api.refresh() is True


@pytest.mark.parametrize('payload,status', [
    ({'error': 'upstream'}, 500),
    (UNAUTHENTICATED, 401),
    (UNAUTHENTICATED, 200),                                   # GraphQL error in a 200
    ({'data': {'vehicleState': None}}, 200),
    ({'data': {'vehicleState': {'chargerStatus': None,
                                'batteryLevel': None,
                                'distanceToEmpty': None}}}, 200),  # service mode
])
def test_refresh_reports_failure(api, gateway, payload, status):
    api.refresh()                                   # a good reading first
    gateway.set_response('GetVehicleState', payload, status_code=status)

    assert api.refresh() is False
    assert api.battery_level == 72.4                # cache untouched


def test_refresh_without_a_vehicle_reports_failure(api, gateway):
    api.vehicle_id = None
    assert api.refresh() is False


# ---------------------------------------------------------------------------
# The background refresher stamps the clock only on success
# ---------------------------------------------------------------------------

class _OneTick(Exception):
    pass


def _run_one_tick(server_module, monkeypatch):
    """Run exactly one iteration of _background_refresher."""
    calls = {'n': 0}

    def sleep(_seconds):
        calls['n'] += 1
        if calls['n'] > 1:
            raise _OneTick

    monkeypatch.setattr(server_module.time, 'sleep', sleep)
    with pytest.raises(_OneTick):
        server_module._background_refresher()


def test_failed_refresh_does_not_advance_the_clock(server_module, server_gateway,
                                                   monkeypatch):
    server_module._last_refresh = 1000.0
    # The 9 Sep failure mode: a non-200 that refresh() used to swallow.
    server_gateway.set_response('GetVehicleState', UNAUTHENTICATED, status_code=401)

    _run_one_tick(server_module, monkeypatch)

    assert server_module._last_refresh == 1000.0


def test_successful_refresh_advances_the_clock(server_module, server_gateway,
                                               monkeypatch):
    server_module._last_refresh = 1000.0

    _run_one_tick(server_module, monkeypatch)

    assert server_module._last_refresh > 1000.0


# ---------------------------------------------------------------------------
# End to end: a dead session becomes visible
# ---------------------------------------------------------------------------

def test_a_dead_session_turns_health_stale(server_module, server_gateway, client,
                                           monkeypatch):
    """Session expired: every refresh fails. Once the last success is older
    than STALE_AFTER, /health says stale and the data endpoints return 503 —
    which is what Home Assistant's "bridge data is stale" alert watches."""
    server_gateway.set_response('GetVehicleState', UNAUTHENTICATED, status_code=401)
    server_module._last_refresh = (server_module.time.time()
                                   - server_module.STALE_AFTER - 60)

    _run_one_tick(server_module, monkeypatch)

    health = client.get('/health').get_json()
    assert health['stale'] is True
    assert health['status'] == 'stale'
    assert client.get('/soc').status_code == 503


def test_recovery_clears_stale(server_module, server_gateway, client, monkeypatch):
    server_module._last_refresh = (server_module.time.time()
                                   - server_module.STALE_AFTER - 60)

    _run_one_tick(server_module, monkeypatch)            # default: success

    assert client.get('/health').get_json()['stale'] is False
