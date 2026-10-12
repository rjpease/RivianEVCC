"""Shared fixtures for the Rivian EVCC Bridge regression suite.

Everything here is offline. No test in this suite may reach the real Rivian
gateway or touch a real vehicle: `_no_real_network` below is an autouse guard
that turns any unstubbed outbound call into an immediate failure.
"""

import json
import sys
from datetime import datetime

import pytest
import requests


# ---------------------------------------------------------------------------
# Safety net
# ---------------------------------------------------------------------------

class RealNetworkAccessError(AssertionError):
    """Raised when a test tries to make a real outbound HTTP call."""


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Fail loudly if any test escapes its stub and calls the real API.

    This project writes charging schedules to a real vehicle, so an accidental
    live call is never an acceptable test failure mode.
    """
    def _blocked(*args, **kwargs):
        raise RealNetworkAccessError(
            'A test attempted a real HTTP request. Stub the gateway instead — '
            'no test may contact the Rivian API.'
        )

    for name in ('post', 'get', 'put', 'patch', 'delete', 'request'):
        monkeypatch.setattr(requests, name, _blocked)
    monkeypatch.setattr(requests.Session, 'request', _blocked)


# ---------------------------------------------------------------------------
# Fake Rivian GraphQL gateway
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    @property
    def text(self):
        return json.dumps(self._payload, default=str)


#: Response bodies for a healthy account, keyed by GraphQL operationName.
DEFAULT_RESPONSES = {
    'CreateCSRFToken': {
        'data': {'createCsrfToken': {
            '__typename': 'CreateCsrfTokenResponse',
            'csrfToken': 'csrf-stub',
            'appSessionToken': 'app-stub',
        }},
    },
    'Login': {
        'data': {'login': {
            '__typename': 'MobileLoginResponse',
            'accessToken': 'access-stub',
            'refreshToken': 'refresh-stub',
            'userSessionToken': 'user-stub',
        }},
    },
    'getUserInfo': {
        'data': {'currentUser': {
            '__typename': 'User',
            'id': 'user-id-stub',
            'vehicles': [{'__typename': 'UserVehicle', 'id': 'vehicle-id-stub'}],
        }},
    },
    'GetVehicleState': {
        'data': {'vehicleState': {
            '__typename': 'VehicleState',
            'batteryLevel': {'timeStamp': '2026-09-06T12:00:00Z', 'value': 72.4},
            'chargerStatus': {'timeStamp': '2026-09-06T12:00:00Z',
                              'value': 'chrgr_sts_connected_charging'},
            'distanceToEmpty': {'timeStamp': '2026-09-06T12:00:00Z', 'value': 250},
        }},
    },
    'GetChargingSchedule': {
        'data': {'getVehicle': {'chargingSchedules': []}},
    },
    'SetChargingSchedule': {
        'data': {'setChargingSchedules': {'success': True}},
    },
}


class FakeGateway:
    """Stands in for `requests.post` against Rivian's GraphQL endpoint.

    Records every request so tests can assert on call counts, ordering and
    exact payloads — the payload is the charging command, so asserting on it
    is how this suite pins charging behaviour.
    """

    def __init__(self):
        self.calls = []
        self._responses = {op: dict(body) for op, body in DEFAULT_RESPONSES.items()}
        self._overrides = {}

    # -- configuration ----------------------------------------------------

    def set_response(self, operation, payload, status_code=200):
        """Override one operation with a payload and/or non-200 status."""
        self._overrides[operation] = FakeResponse(payload, status_code)

    def set_schedules(self, schedules):
        """Set what GetChargingSchedule returns."""
        self._responses['GetChargingSchedule'] = {
            'data': {'getVehicle': {'chargingSchedules': schedules}},
        }
        self._overrides.pop('GetChargingSchedule', None)

    # -- transport --------------------------------------------------------

    def post(self, url, headers=None, json=None, **kwargs):
        request = json or {}
        operation = request.get('operationName')
        self.calls.append({
            'url': url,
            'headers': headers or {},
            'operation': operation,
            'variables': request.get('variables'),
            'kwargs': kwargs,
        })
        if operation in self._overrides:
            return self._overrides[operation]
        if operation not in self._responses:
            raise AssertionError(f'FakeGateway has no response for {operation!r}')
        return FakeResponse(self._responses[operation])

    # -- assertions -------------------------------------------------------

    def operations(self):
        return [call['operation'] for call in self.calls]

    def calls_for(self, operation):
        return [call for call in self.calls if call['operation'] == operation]

    def schedules_written(self):
        """Every schedule list sent to SetChargingSchedule, in order."""
        return [call['variables']['chargingSchedules']
                for call in self.calls_for('SetChargingSchedule')]

    def last_schedule_written(self):
        written = self.schedules_written()
        assert written, 'no charging schedule was written'
        assert len(written[-1]) == 1, 'expected exactly one schedule in the write'
        return written[-1][0]

    def reset(self):
        self.calls.clear()


@pytest.fixture
def gateway(monkeypatch):
    """A FakeGateway wired into rivian_api's `requests.post`."""
    import rivian_api

    fake = FakeGateway()
    monkeypatch.setattr(rivian_api.requests, 'post', fake.post)
    return fake


# ---------------------------------------------------------------------------
# Clock control
# ---------------------------------------------------------------------------

def freeze_clock(monkeypatch, module, when):
    """Pin `module.datetime.now()` to `when`.

    Both rivian_api and server do `from datetime import datetime`, so the name
    is rebound on the module rather than on the datetime package.
    """
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return when

    monkeypatch.setattr(module, 'datetime', FrozenDatetime)
    return when


@pytest.fixture
def at_clock(monkeypatch):
    """Factory: `at_clock(module, hour=9)` freezes that module's clock."""
    def _freeze(module, hour=9, minute=0, day=6):
        return freeze_clock(monkeypatch, module,
                            datetime(2026, 9, day, hour, minute, 0))
    return _freeze


# ---------------------------------------------------------------------------
# Application objects
# ---------------------------------------------------------------------------

@pytest.fixture
def config_file(tmp_path):
    """A minimal valid config.json."""
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({
        'rivian-user': 'test@example.invalid',
        'rivian-pass': 'not-a-real-password',
        'cache-ttl-seconds': 300,
        'bridge-port': 7071,
    }))
    return path


@pytest.fixture
def session_file(tmp_path):
    """A stored session, as RivianSessionInitOTP.py would write it."""
    path = tmp_path / 'rivian-session.json'
    path.write_text(json.dumps({
        'appSessionToken': 'stored-app',
        'userSessionToken': 'stored-user',
        'csrfToken': 'stored-csrf',
    }))
    return path


#: Where the fixtures' bridge has last seen the car's schedule. On the real
#: car a schedule with a location always exists (the bridge always writes one),
#: so a running bridge has always seen one. Tests of the E-003 "no location
#: seen yet" path set `last_known_location = None` explicitly.
SEEN_LOCATION = {'latitude': 37.7, 'longitude': -122.4}


@pytest.fixture
def api(gateway, config_file, session_file):
    """A logged-in RivianAPI backed by the fake gateway.

    Construction runs the real `login()` path against the stub, so the fixture
    itself exercises stored-session loading and vehicle-ID discovery.
    """
    from config import Config
    from rivian_api import RivianAPI

    instance = RivianAPI(Config(str(config_file)), session_file=str(session_file))
    assert instance.vehicle_id == 'vehicle-id-stub'
    instance.last_known_location = dict(SEEN_LOCATION)
    gateway.reset()
    return instance


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

class _DummyThread:
    """Stands in for threading.Thread so importing server starts no daemons."""

    def __init__(self, *args, **kwargs):
        self.target = kwargs.get('target')

    def start(self):
        return None


@pytest.fixture
def server_module(monkeypatch, tmp_path, config_file, session_file):
    """Import `server` with its module-level bootstrap driven by the stub.

    server.py performs live I/O and starts two daemon threads at import time
    (finding C-005), so it has to be imported inside a prepared environment
    rather than at collection. When C-005 is fixed this fixture should get
    simpler, not disappear.
    """
    import threading

    import rivian_api

    fake = FakeGateway()
    monkeypatch.setattr(rivian_api.requests, 'post', fake.post)
    monkeypatch.setattr(threading, 'Thread', _DummyThread)

    # server.py reads config.json and rivian-session.json from its cwd.
    for name in ('config.json', 'rivian-session.json'):
        (tmp_path / name).write_text(
            (config_file if name == 'config.json' else session_file).read_text()
        )
    monkeypatch.chdir(tmp_path)

    sys.modules.pop('server', None)
    import server  # noqa: E402  (deliberately imported under the stub)

    server.app.config['TESTING'] = True
    server.rivian.last_known_location = dict(SEEN_LOCATION)
    fake.reset()
    server.gateway = fake
    yield server
    sys.modules.pop('server', None)


@pytest.fixture
def client(server_module):
    return server_module.app.test_client()


@pytest.fixture
def server_gateway(server_module):
    return server_module.gateway


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALL_WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday',
                'Friday', 'Saturday', 'Sunday']


def charging_schedule(amperage, start_time=0, duration=1440, location=None):
    """The shape the bridge writes for an 'enabled' schedule."""
    return {
        'startTime': start_time,
        'duration': duration,
        'location': {} if location is None else location,
        'amperage': amperage,
        'enabled': True,
        'weekDays': list(ALL_WEEKDAYS),
    }


def existing_schedule(amperage, start_time=0, duration=1440, enabled=True,
                      location=None):
    """The shape GetChargingSchedule returns for an existing schedule."""
    return {
        'startTime': start_time,
        'duration': duration,
        'location': {'latitude': 37.7, 'longitude': -122.4} if location is None
                    else location,
        'amperage': amperage,
        'enabled': enabled,
        'weekDays': list(ALL_WEEKDAYS),
    }


def pytest_report_header(config):
    return ('rivian-evcc-bridge: offline regression suite — '
            'no test contacts the Rivian API')
