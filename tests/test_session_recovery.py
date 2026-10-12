"""Baseline: recovering from a session that expires at runtime (C-028).

Before the fix the refresher re-read rivian-session.json only when the bridge
had failed at startup. A session that expired while the bridge was running
kept failing with the old in-memory tokens, so running RivianSessionInitOTP.py
did nothing until the bridge was restarted, and /health kept saying
"auth": "ok".

Now: a failed refresh re-reads the session file if it has changed since it was
loaded, then refreshes again. Never a password login, and no extra Rivian
calls while the file is unchanged.
"""

import json
import os

import pytest

from conftest import FakeResponse

pytestmark = pytest.mark.baseline

UNAUTHENTICATED = {'data': None, 'errors': [
    {'extensions': {'code': 'UNAUTHENTICATED'}, 'message': 'User is unauthenticated'}]}
TOKEN_CHECKED = ('GetVehicleState', 'getUserInfo', 'GetChargingSchedule')


class _OneTick(Exception):
    pass


def _tick(server, monkeypatch):
    """Run exactly one iteration of _background_refresher."""
    calls = {'n': 0}

    def sleep(_seconds):
        calls['n'] += 1
        if calls['n'] > 1:
            raise _OneTick

    monkeypatch.setattr(server.time, 'sleep', sleep)
    with pytest.raises(_OneTick):
        server._background_refresher()


@pytest.fixture
def rivian_tokens(server_module, monkeypatch):
    """Rivian accepts only the token in `valid['u-sess']`; anything else is
    rejected as UNAUTHENTICATED, like an expired session."""
    import rivian_api

    valid = {'u-sess': 'stored-user'}            # what the fixture session holds
    inner = rivian_api.requests.post
    gateway = server_module.gateway

    def post(url, headers=None, json=None, **kwargs):
        op = (json or {}).get('operationName')
        if op in TOKEN_CHECKED and (headers or {}).get('u-sess') != valid['u-sess']:
            gateway.calls.append({'url': url, 'headers': headers or {}, 'operation': op,
                                  'variables': (json or {}).get('variables'),
                                  'kwargs': kwargs})
            return FakeResponse(UNAUTHENTICATED, 401)
        return inner(url, headers=headers, json=json, **kwargs)

    monkeypatch.setattr(rivian_api.requests, 'post', post)
    return valid


def _write_new_session(token, bump=10):
    """What RivianSessionInitOTP.py does, with a clearly newer mtime."""
    with open('rivian-session.json', 'w') as f:
        json.dump({'appSessionToken': 'new-app', 'userSessionToken': token,
                   'csrfToken': 'new-csrf'}, f)
    st = os.stat('rivian-session.json')
    os.utime('rivian-session.json', (st.st_atime + bump, st.st_mtime + bump))


def _expire(server, valid):
    valid['u-sess'] = 'nobody-has-this'
    server._last_refresh = server.time.time() - server.STALE_AFTER - 60


def test_expiry_is_reported_as_stale_and_expired(server_module, rivian_tokens,
                                                 client, monkeypatch):
    _expire(server_module, rivian_tokens)

    _tick(server_module, monkeypatch)

    health = client.get('/health').get_json()
    assert health['stale'] is True
    assert health['auth'] == 'expired'


def test_no_extra_calls_while_the_session_file_is_unchanged(server_module,
                                                           rivian_tokens,
                                                           monkeypatch):
    _expire(server_module, rivian_tokens)
    server_module.gateway.reset()

    for _ in range(3):
        _tick(server_module, monkeypatch)

    ops = server_module.gateway.operations()
    assert ops == ['GetVehicleState'] * 3          # no getUserInfo, no Login


def test_otp_script_is_picked_up_without_a_restart(server_module, rivian_tokens,
                                                   client, monkeypatch):
    _expire(server_module, rivian_tokens)
    _tick(server_module, monkeypatch)                  # fails, stale

    # The owner runs RivianSessionInitOTP.py; Rivian accepts the new token.
    _write_new_session('fresh-user')
    rivian_tokens['u-sess'] = 'fresh-user'
    server_module.gateway.reset()

    _tick(server_module, monkeypatch)

    health = client.get('/health').get_json()
    assert health['stale'] is False
    assert health['auth'] == 'ok'
    ops = server_module.gateway.operations()
    assert ops[:3] == ['GetVehicleState', 'getUserInfo', 'GetVehicleState']
    assert 'Login' not in ops and 'CreateCSRFToken' not in ops


def test_a_rejected_new_file_is_tried_once_not_every_tick(server_module,
                                                         rivian_tokens,
                                                         monkeypatch):
    _expire(server_module, rivian_tokens)
    _write_new_session('also-rejected')               # e.g. wrong OTP run
    server_module.gateway.reset()

    for _ in range(3):
        _tick(server_module, monkeypatch)

    assert server_module.gateway.operations().count('getUserInfo') == 1
    assert 'Login' not in server_module.gateway.operations()


def test_a_renewal_while_healthy_is_used_when_the_old_session_dies(
        server_module, rivian_tokens, client, monkeypatch):
    """OTP script run early: the old session keeps working until it expires,
    then the bridge switches to the new file on the failing tick."""
    _write_new_session('renewed-user')
    _tick(server_module, monkeypatch)                  # old token still valid
    assert client.get('/health').get_json()['auth'] == 'ok'

    rivian_tokens['u-sess'] = 'renewed-user'           # old one now expired
    server_module.gateway.reset()
    _tick(server_module, monkeypatch)

    assert client.get('/health').get_json()['stale'] is False
    assert server_module.rivian.user_session_token == 'renewed-user'


def test_graphql_unauthenticated_in_a_200_also_marks_expired(api, gateway):
    gateway.set_response('GetVehicleState', UNAUTHENTICATED, status_code=200)

    assert api.refresh() is False
    assert api.auth_state == 'expired'


def test_a_successful_refresh_restores_auth_ok(api, gateway):
    api.auth_state = 'expired'

    assert api.refresh() is True
    assert api.auth_state == 'ok'
