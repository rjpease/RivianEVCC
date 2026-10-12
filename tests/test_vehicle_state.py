"""Baseline: vehicle-state parsing, the session/auth path, and config loading."""

import json

import pytest

from config import Config
from rivian_api import RivianAPI

pytestmark = pytest.mark.baseline


# ---------------------------------------------------------------------------
# refresh()
# ---------------------------------------------------------------------------

def test_refresh_populates_the_cache(api, gateway):
    api.refresh()

    assert api.battery_level == 72.4
    assert api.charging_status == 'chrgr_sts_connected_charging'
    assert api.get_soc() == 72.4
    assert api.get_evcc_status() == 'C'


def test_refresh_asks_for_the_discovered_vehicle(api, gateway):
    api.refresh()

    call = gateway.calls_for('GetVehicleState')[-1]
    assert call['variables'] == {'vehicleID': 'vehicle-id-stub'}


def test_range_is_passed_through_as_km(api, gateway):
    """distanceToEmpty is already in km. Verified 2026-10-12 on the owner's
    R1S (car set to imperial): /range read 451 while the Rivian app showed
    280 mi, a ratio of 1.61. The old x1.60934 conversion double-converted."""
    api.refresh()

    assert api.get_range_km() == 250


def test_missing_charger_status_leaves_the_cache_untouched(api, gateway):
    """A vehicle in service mode returns a state without chargerStatus."""
    api.refresh()
    gateway.set_response('GetVehicleState', {
        'data': {'vehicleState': {'chargerStatus': None,
                                  'batteryLevel': None,
                                  'distanceToEmpty': None}},
    })

    api.refresh()

    assert api.battery_level == 72.4
    assert api.charging_status == 'chrgr_sts_connected_charging'


def test_refresh_without_a_vehicle_id_makes_no_call(api, gateway):
    api.vehicle_id = None

    api.refresh()

    assert gateway.calls_for('GetVehicleState') == []


@pytest.mark.defect
def test_failed_refresh_serves_the_previous_values(api, gateway):
    """Finding C-009: a non-200 refresh returns silently, so the stale cache is
    served indefinitely with nothing marking it stale."""
    api.refresh()
    gateway.set_response('GetVehicleState', {'error': 'upstream'}, status_code=500)

    api.refresh()

    assert api.get_soc() == 72.4       # still the old value, not None


# ---------------------------------------------------------------------------
# Session handling
# ---------------------------------------------------------------------------

def test_stored_session_is_used_without_re_authenticating(gateway, config_file,
                                                          session_file):
    RivianAPI(Config(str(config_file)), session_file=str(session_file))

    assert gateway.operations() == ['getUserInfo']


def test_stored_session_headers_are_sent(gateway, config_file, session_file):
    RivianAPI(Config(str(config_file)), session_file=str(session_file))

    headers = gateway.calls_for('getUserInfo')[0]['headers']
    assert headers['a-sess'] == 'stored-app'
    assert headers['u-sess'] == 'stored-user'
    assert headers['csrf-token'] == 'stored-csrf'
    assert headers['apollographql-client-name'] == 'com.rivian.android.consumer'


def test_missing_session_file_triggers_a_full_login(gateway, config_file,
                                                    tmp_path):
    """With no tokens, init_user_info() short-circuits without a call, so the
    sequence is CSRF -> Login -> getUserInfo."""
    missing = tmp_path / 'absent-session.json'

    instance = RivianAPI(Config(str(config_file)), session_file=str(missing))

    assert gateway.operations() == ['CreateCSRFToken', 'Login', 'getUserInfo']
    assert instance.vehicle_id == 'vehicle-id-stub'


def test_a_fresh_login_is_persisted_to_disk(gateway, config_file, tmp_path):
    session_path = tmp_path / 'new-session.json'

    RivianAPI(Config(str(config_file)), session_file=str(session_path))

    stored = json.loads(session_path.read_text())
    assert stored == {
        'appSessionToken': 'app-stub',
        'userSessionToken': 'user-stub',
        'csrfToken': 'csrf-stub',
    }


def test_login_sends_the_configured_credentials(gateway, config_file, tmp_path):
    RivianAPI(Config(str(config_file)),
              session_file=str(tmp_path / 'absent.json'))

    assert gateway.calls_for('Login')[0]['variables'] == {
        'email': 'test@example.invalid',
        'password': 'not-a-real-password',
    }


def test_vehicle_id_comes_from_the_first_vehicle(gateway, config_file,
                                                 session_file):
    gateway.set_response('getUserInfo', {'data': {'currentUser': {
        'id': 'u', 'vehicles': [{'id': 'first'}, {'id': 'second'}]}}})

    instance = RivianAPI(Config(str(config_file)), session_file=str(session_file))

    assert instance.vehicle_id == 'first'


def test_account_with_no_vehicles_is_reported_not_raised(gateway, config_file,
                                                         session_file):
    gateway.set_response('getUserInfo',
                         {'data': {'currentUser': {'id': 'u', 'vehicles': []}}})

    instance = RivianAPI(Config(str(config_file)), session_file=str(session_file))

    assert instance.vehicle_id is None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_config_reads_all_fields(config_file):
    config = Config(str(config_file))

    assert config.rivian_user == 'test@example.invalid'
    assert config.rivian_pass == 'not-a-real-password'
    assert config.cache_ttl == 300
    assert config.port == 7071


def test_config_defaults_match_the_documented_port(tmp_path):
    """Finding C-011: the default must agree with the README and the shipped
    EVCC configuration, all of which use 7071."""
    path = tmp_path / 'minimal.json'
    path.write_text(json.dumps({'rivian-user': 'a@b.invalid', 'rivian-pass': 'x'}))

    config = Config(str(path))

    assert config.port == 7071
    assert config.cache_ttl == 300


def test_config_coerces_numeric_strings(tmp_path):
    path = tmp_path / 'strings.json'
    path.write_text(json.dumps({'rivian-user': 'a@b.invalid', 'rivian-pass': 'x',
                                'bridge-port': '8080', 'cache-ttl-seconds': '60'}))

    config = Config(str(path))

    assert config.port == 8080
    assert config.cache_ttl == 60


@pytest.mark.defect
@pytest.mark.parametrize('payload,expected', [
    ({}, KeyError),
    ({'rivian-user': 'a@b.invalid'}, KeyError),
    ({'rivian-user': 'a@b.invalid', 'rivian-pass': 'x', 'bridge-port': 'http'},
     ValueError),
])
def test_invalid_config_raises_without_explanation(tmp_path, payload, expected):
    """Finding C-005: config errors surface as bare KeyError/ValueError out of
    module import rather than an actionable message."""
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(payload))

    with pytest.raises(expected):
        Config(str(path))
