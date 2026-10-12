"""Baseline: the bridge survives Rivian auth and network failures (C-001, C-005).

C-001: an MFA account's password login returns MobileMFALoginResponse; the
code indexed userSessionToken and raised KeyError. C-005: that, a network
error, or a GraphQL error at import time killed the process, and launchd's
KeepAlive restarted it every ~10 s — each restart a fresh password login that
can text an MFA code to the owner.

Now: no Rivian failure stops the bridge, a password login is tried at most
once per process and never after a network error, and the background
refresher reconnects using the stored session file only.
"""

import json

import pytest
import requests

from config import Config
from rivian_api import RivianAPI

pytestmark = pytest.mark.baseline

UNAUTHENTICATED = {'data': None, 'errors': [
    {'extensions': {'code': 'UNAUTHENTICATED'}, 'message': 'User is unauthenticated'}]}
MFA = {'data': {'login': {'__typename': 'MobileMFALoginResponse', 'otpToken': 'otp-stub'}}}


def _reject_stored_session(gateway):
    gateway.set_response('getUserInfo', UNAUTHENTICATED, status_code=401)


def _api(config_file, session_file):
    return RivianAPI(Config(str(config_file)), session_file=str(session_file))


# ---------------------------------------------------------------------------
# C-001 — MFA and other login failures don't raise
# ---------------------------------------------------------------------------

def test_mfa_login_does_not_crash(gateway, config_file, session_file):
    _reject_stored_session(gateway)
    gateway.set_response('Login', MFA)
    before = session_file.read_text()

    api = _api(config_file, session_file)

    assert api.auth_state == 'needs_otp'
    assert api.vehicle_id is None
    assert session_file.read_text() == before        # good file never clobbered


def test_password_login_is_tried_only_once(gateway, config_file, session_file):
    _reject_stored_session(gateway)
    gateway.set_response('Login', MFA)
    api = _api(config_file, session_file)

    api.login()
    api.login()

    assert len(gateway.calls_for('Login')) == 1


def test_network_error_never_triggers_a_password_login(gateway, config_file,
                                                       session_file):
    gateway.set_error('getUserInfo', requests.ConnectionError('no route to host'))

    api = _api(config_file, session_file)

    assert api.auth_state == 'failed'
    assert gateway.calls_for('Login') == []


@pytest.mark.parametrize('payload,status', [
    (UNAUTHENTICATED, 200),                  # GraphQL error in a 200 (C-002)
    ({'data': {'currentUser': {'vehicles': []}}}, 200),
])
def test_unusable_user_info_does_not_crash(gateway, config_file, session_file,
                                           payload, status):
    gateway.set_response('getUserInfo', payload, status_code=status)
    gateway.set_response('Login', MFA)

    api = _api(config_file, session_file)

    assert api.vehicle_id is None


def test_corrupt_session_file_does_not_crash(gateway, config_file, tmp_path):
    bad = tmp_path / 'rivian-session.json'
    bad.write_text('{not json')

    api = _api(config_file, bad)                     # falls back to one login

    assert api.auth_state == 'ok'
    assert len(gateway.calls_for('Login')) == 1


def test_csrf_request_has_a_timeout(gateway, config_file, tmp_path):
    _api(config_file, tmp_path / 'absent.json')

    assert gateway.calls_for('CreateCSRFToken')[0]['kwargs'].get('timeout') == 20


# ---------------------------------------------------------------------------
# reconnect() — stored session only, picks up a new OTP session
# ---------------------------------------------------------------------------

def test_reconnect_never_uses_the_password(gateway, config_file, session_file):
    _reject_stored_session(gateway)
    gateway.set_response('Login', MFA)
    api = _api(config_file, session_file)
    gateway.reset()

    for _ in range(5):
        assert api.reconnect() is False

    assert gateway.calls_for('Login') == []
    assert gateway.calls_for('CreateCSRFToken') == []


def test_reconnect_picks_up_a_new_otp_session(gateway, config_file, session_file):
    _reject_stored_session(gateway)
    gateway.set_response('Login', MFA)
    api = _api(config_file, session_file)

    # The owner runs RivianSessionInitOTP.py; Rivian accepts the new tokens.
    session_file.write_text(json.dumps({'appSessionToken': 'new-app',
                                        'userSessionToken': 'new-user',
                                        'csrfToken': 'new-csrf'}))
    gateway._overrides.pop('getUserInfo')

    assert api.reconnect() is True
    assert api.auth_state == 'ok'
    assert api.vehicle_id == 'vehicle-id-stub'
    assert gateway.calls_for('getUserInfo')[-1]['headers']['u-sess'] == 'new-user'


def test_no_schedule_calls_without_a_vehicle(api, gateway):
    api.vehicle_id = None

    assert api.get_schedules() is None
    assert api.get_current_amps() == 0
    assert gateway.calls == []


# ---------------------------------------------------------------------------
# C-005 — the server starts anyway, reports the problem, and recovers
# ---------------------------------------------------------------------------

def _rivian_down(fake):
    fake.set_error('getUserInfo', requests.ConnectionError('no route to host'))


def test_server_starts_when_rivian_is_unreachable(server_factory):
    server = server_factory(_rivian_down)
    client = server.app.test_client()

    health = client.get('/health')
    assert health.status_code == 200
    body = health.get_json()
    assert body['stale'] is True
    assert body['auth'] == 'failed'
    assert body['charger_enabled'] is None
    assert client.get('/soc').status_code == 503
    assert client.post('/charger/enable', data='false').status_code == 503
    assert server.gateway.calls_for('GetChargingSchedule') == []
    assert server.gateway.calls_for('Login') == []


def test_server_starts_on_an_mfa_account_with_a_dead_session(server_factory):
    def dead_session(fake):
        _reject_stored_session(fake)
        fake.set_response('Login', MFA)

    server = server_factory(dead_session)

    body = server.app.test_client().get('/health').get_json()
    assert body['auth'] == 'needs_otp'
    assert body['stale'] is True


class _OneTick(Exception):
    pass


def _one_refresher_tick(server, monkeypatch):
    calls = {'n': 0}

    def sleep(_seconds):
        calls['n'] += 1
        if calls['n'] > 1:
            raise _OneTick

    monkeypatch.setattr(server.time, 'sleep', sleep)
    with pytest.raises(_OneTick):
        server._background_refresher()


def test_refresher_reconnects_when_rivian_comes_back(server_factory, monkeypatch):
    server = server_factory(_rivian_down)
    server.gateway._errors.clear()                   # network is back

    _one_refresher_tick(server, monkeypatch)

    body = server.app.test_client().get('/health').get_json()
    assert body['auth'] == 'ok'
    assert body['stale'] is False
    assert server.gateway.calls_for('Login') == []   # stored session only


def test_refresher_keeps_trying_without_the_password(server_factory, monkeypatch):
    server = server_factory(_rivian_down)
    server.gateway.reset()

    for _ in range(3):
        _one_refresher_tick(server, monkeypatch)

    assert len(server.gateway.calls_for('getUserInfo')) == 3
    assert server.gateway.calls_for('Login') == []
    assert server.gateway.calls_for('GetVehicleState') == []


def test_window_shifter_waits_while_disconnected(server_factory, monkeypatch):
    from datetime import datetime

    server = server_factory(_rivian_down)
    server.gateway.reset()
    ticks = iter([datetime(2026, 9, 6, h, 0) for h in (12, 13, 0)])

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls._now

    def sleep(_seconds):
        try:
            Clock._now = next(ticks)
        except StopIteration:
            raise _OneTick from None

    monkeypatch.setattr(server, 'datetime', Clock)
    monkeypatch.setattr(server.time, 'sleep', sleep)
    with pytest.raises(_OneTick):
        server._noon_window_shifter()

    assert server.gateway.calls == []
