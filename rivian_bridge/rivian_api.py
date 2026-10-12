# Rivian API — vehicle state reader + charging schedule controller for EVCC bridge
# Adapted from github.com/ostap-korkuna/rivian-charging-automation

import functools
import json
import logging
import os
import threading
import requests
from datetime import datetime

logger = logging.getLogger(__name__)


def _valid_location(location):
    """True if `location` is a usable {latitude, longitude} pair."""
    return (isinstance(location, dict)
            and location.get('latitude') is not None
            and location.get('longitude') is not None)


def _schedule_locked(method):
    """Run `method` holding the instance's schedule lock (CH-002)."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.schedule_lock:
            return method(self, *args, **kwargs)
    return wrapper


class RivianAPI:
    GATEWAY_URL = 'https://rivian.com/api/gql/gateway/graphql'

    AMPS_MAX = 48
    AMPS_MIN = 8
    # Amperage of the parked 1-hour "off" window. If the window is ever not
    # moved before it comes round, the car charges at this rate, so keep it at
    # the minimum.
    AMPS_PARK = AMPS_MIN

    # Rivian charger status values
    STATUS_CONNECTED_CHARGING = 'chrgr_sts_connected_charging'
    STATUS_CONNECTED_NO_CHARGE = 'chrgr_sts_connected_no_chrg'

    def __init__(self, config, session_file='rivian-session.json'):
        self.config = config
        self.session_file = session_file
        self.app_session_token = None
        self.user_session_token = None
        self.csrf_token = None
        self.vehicle_id = None

        # 'ok' once a vehicle is found; 'needs_otp' if Rivian wants MFA (run
        # RivianSessionInitOTP.py); 'failed' if Rivian rejected or couldn't be
        # reached. Reported by /health.
        self.auth_state = 'failed'
        # A password login can make Rivian text an MFA code, so try it at most
        # once per process (C-001).
        self._password_login_tried = False

        # Serialises every charging-schedule read-modify-write, so EVCC's
        # requests and the window shifter can't interleave between a read and
        # the write that depends on it (C-007). Re-entrant because
        # enable_charging -> set_charge_amps -> _apply_schedule nest.
        self.schedule_lock = threading.RLock()

        # Location of the car's schedule, as last read from Rivian. Used when a
        # read fails just before a write, so no schedule is ever sent with an
        # empty location (E-003).
        self.last_known_location = None

        # Cached vehicle state (populated by refresh())
        self.charging_status = None
        self.battery_level = None
        self.distance_to_empty_km = None

        self.login()

    # ------------------------------------------------------------------
    # Session / authentication
    # ------------------------------------------------------------------

    def _headers(self):
        h = {
            'Content-Type': 'application/json',
            'apollographql-client-name': 'com.rivian.android.consumer',
        }
        if self.app_session_token:
            h['a-sess'] = self.app_session_token
        if self.user_session_token:
            h['u-sess'] = self.user_session_token
        if self.csrf_token:
            h['csrf-token'] = self.csrf_token
        return h

    def _post(self, url, request):
        return requests.post(url, headers=self._headers(), json=request, timeout=20)

    def init_session(self):
        """Password login. Returns True on success, 'mfa' if Rivian requires
        an MFA code (not possible unattended), False on any other failure."""
        logger.info('Initializing new Rivian session...')
        resp = requests.post(
            self.GATEWAY_URL,
            headers={'Content-Type': 'application/json'},
            json={
                "operationName": "CreateCSRFToken",
                "variables": [],
                "query": "mutation CreateCSRFToken { createCsrfToken { __typename csrfToken appSessionToken } }"
            },
            timeout=20,
        )
        if resp.status_code != 200:
            logger.error('CSRF request failed: %s', resp.text)
            return False

        try:
            csrf = resp.json()['data']['createCsrfToken']
            csrf_token, app_session_token = csrf['csrfToken'], csrf['appSessionToken']
        except (KeyError, TypeError, ValueError):
            logger.error('CSRF response not understood: %s', resp.text[:300])
            return False
        self.csrf_token = csrf_token
        self.app_session_token = app_session_token

        resp = self._post(self.GATEWAY_URL, {
            "operationName": "Login",
            "variables": {
                "email": self.config.rivian_user,
                "password": self.config.rivian_pass,
            },
            "query": (
                "mutation Login($email: String!, $password: String!) { "
                "login(email: $email, password: $password) { "
                "__typename "
                "... on MobileLoginResponse { accessToken refreshToken userSessionToken } "
                "... on MobileMFALoginResponse { otpToken } } }"
            )
        })
        if resp.status_code != 200:
            logger.error('Login failed: %s', resp.status_code)
            return False

        try:
            result = (resp.json().get('data') or {}).get('login') or {}
        except (ValueError, AttributeError):
            result = {}
        if result.get('__typename') == 'MobileMFALoginResponse' or 'otpToken' in result:
            # C-001: an MFA account can't log in unattended. Don't crash.
            return 'mfa'
        if not result.get('userSessionToken'):
            logger.error('Login response not understood (status %s)', resp.status_code)
            return False
        self.user_session_token = result['userSessionToken']
        logger.info('Rivian session initialized')
        return True

    def init_user_info(self):
        if not self.app_session_token:
            return False
        resp = self._post(self.GATEWAY_URL, {
            "operationName": "getUserInfo",
            "variables": {},
            "query": "query getUserInfo { currentUser { __typename id vehicles { __typename id } } }"
        })
        if resp.status_code != 200:
            return False
        try:
            self.vehicle_id = resp.json()['data']['currentUser']['vehicles'][0]['id']
            logger.info('Vehicle ID: %s', self.vehicle_id)
            return True
        except (KeyError, IndexError, TypeError, ValueError) as e:
            logger.error('Failed to parse vehicle ID (session rejected?): %r', e)
            return False

    def _load_stored_session(self):
        """Load tokens from the session file. Returns True if it was usable."""
        if not os.path.exists(self.session_file):
            return False
        logger.info('Loading stored session from %s', self.session_file)
        try:
            with open(self.session_file) as f:
                d = json.load(f)
            self.app_session_token = d['appSessionToken']
            self.user_session_token = d['userSessionToken']
            self.csrf_token = d['csrfToken']
            return True
        except (OSError, ValueError, KeyError, TypeError) as e:
            logger.error('Session file %s is unreadable: %r', self.session_file, e)
            return False

    def login(self):
        """Connect to Rivian. Never raises; returns True if a vehicle was found.

        Uses the stored session. A password login is tried only if Rivian
        actively rejects that session (not on a network error), and at most
        once per process, because on an MFA account each attempt can text a
        code to the owner (C-001).
        """
        try:
            self._load_stored_session()
            if self.init_user_info():
                self.auth_state = 'ok'
                return True
            if self._password_login_tried:
                self.auth_state = 'failed'
                return False
            self._password_login_tried = True
            logger.info('Stored session invalid — trying a password login (once)')
            result = self.init_session()
            if result == 'mfa':
                self.auth_state = 'needs_otp'
                logger.error('Rivian requires an MFA code: run RivianSessionInitOTP.py. '
                             'The bridge picks up the new session at its next '
                             'refresh, no restart needed.')
                return False
            if result is True and self.init_user_info():
                self._persist_session()
                self.auth_state = 'ok'
                return True
            self.auth_state = 'failed'
            logger.error('Rivian auth failed. Run RivianSessionInitOTP.py')
            return False
        except requests.RequestException as e:
            # Network not up yet (e.g. right after login): no password retry.
            self.auth_state = 'failed'
            logger.error('Cannot reach Rivian: %r — will retry', e)
            return False
        except Exception:
            self.auth_state = 'failed'
            logger.exception('Unexpected error connecting to Rivian — will retry')
            return False

    def reconnect(self):
        """Retry with the stored session only — never a password login, so it
        is safe to call repeatedly. Picks up a session written by
        RivianSessionInitOTP.py without a restart."""
        try:
            if self._load_stored_session() and self.init_user_info():
                self.auth_state = 'ok'
                logger.info('Reconnected to Rivian')
                return True
        except requests.RequestException as e:
            logger.error('Cannot reach Rivian: %r — will retry', e)
        return False

    def _persist_session(self):
        with open(self.session_file, 'w') as f:
            json.dump({
                'appSessionToken': self.app_session_token,
                'userSessionToken': self.user_session_token,
                'csrfToken': self.csrf_token,
            }, f)
        os.chmod(self.session_file, 0o600)  # holds Rivian session tokens
        logger.info('Session persisted to %s', self.session_file)

    # ------------------------------------------------------------------
    # Vehicle state (EVCC vehicle plugin)
    # ------------------------------------------------------------------

    def refresh(self):
        """Fetch the latest vehicle state from Rivian cloud. Updates cached fields.

        Returns True only if new vehicle state was received, so callers can
        tell fresh data from a failed refresh (C-009).
        """
        if not self.vehicle_id:
            logger.error('No vehicle ID — cannot refresh')
            return False

        resp = self._post(self.GATEWAY_URL, {
            "operationName": "GetVehicleState",
            "variables": {"vehicleID": self.vehicle_id},
            "query": (
                "query GetVehicleState($vehicleID: String!) { "
                "vehicleState(id: $vehicleID) { "
                "__typename "
                "batteryLevel { __typename timeStamp value } "
                "chargerStatus { __typename timeStamp value } "
                "distanceToEmpty { __typename timeStamp value } "
                "} }"
            )
        })

        if resp.status_code != 200:
            logger.error('GetVehicleState failed: %s', resp.text)
            return False

        # GraphQL errors (e.g. UNAUTHENTICATED) can arrive as HTTP 200 with
        # data: null (C-002).
        state = ((resp.json() or {}).get('data') or {}).get('vehicleState') or {}
        if state.get('chargerStatus') is None:
            logger.warning('Vehicle state missing — session expired, API error '
                           'or service mode: %s', resp.text[:300])
            return False

        self.charging_status = state['chargerStatus']['value']
        self.battery_level = state['batteryLevel']['value']

        dte = state.get('distanceToEmpty')
        if dte:
            self.distance_to_empty_km = round(dte['value'])

        logger.info('Refreshed: status=%s soc=%.1f%% range=%s km',
                    self.charging_status, self.battery_level, self.distance_to_empty_km)
        return True

    def get_soc(self):
        return self.battery_level

    def get_evcc_status(self):
        """Map Rivian charger status → EVCC IEC 61851 char (A/B/C)."""
        if self.charging_status == self.STATUS_CONNECTED_CHARGING:
            return 'C'
        if self.charging_status in (
            self.STATUS_CONNECTED_NO_CHARGE,
            'chrgr_sts_connected_not_charging',
        ):
            return 'B'
        return 'A'

    def get_range_km(self):
        return self.distance_to_empty_km

    def is_charging(self):
        return self.charging_status == self.STATUS_CONNECTED_CHARGING

    # ------------------------------------------------------------------
    # Charging schedule control (EVCC charger plugin)
    # ------------------------------------------------------------------

    def get_schedules(self):
        if not self.vehicle_id:
            return None                     # not connected to Rivian yet
        resp = self._post(self.GATEWAY_URL, {
            "operationName": "GetChargingSchedule",
            "variables": {"vehicleId": self.vehicle_id},
            "query": (
                "query GetChargingSchedule($vehicleId: String!) { "
                "getVehicle(id: $vehicleId) { "
                "chargingSchedules { startTime duration location { latitude longitude } "
                "amperage enabled weekDays } } }"
            )
        })
        if resp.status_code != 200:
            logger.error('GetChargingSchedule failed: %s', resp.text)
            return None
        schedules = resp.json()['data']['getVehicle']['chargingSchedules']
        if schedules and _valid_location(schedules[0].get('location')):
            self.last_known_location = dict(schedules[0]['location'])
        return schedules

    @_schedule_locked
    def get_current_amps(self):
        """Return active schedule amperage, or 0 if charging is disabled."""
        return self.read_current_amps() or 0

    @_schedule_locked
    def read_current_amps(self):
        """Like get_current_amps(), but None if the schedule couldn't be read,
        so callers can tell "off" from "unknown" (CH-006)."""
        schedules = self.get_schedules()
        if schedules is None:
            return None
        if not schedules:
            return 0
        s = schedules[0]
        # A 24-hour schedule (duration=1440) means charging is enabled
        if s.get('duration', 0) == 1440 and s.get('enabled', False):
            return s.get('amperage', 0)
        return 0

    @_schedule_locked
    def _apply_schedule(self, schedule):
        """Write `schedule`. Returns True if the car now has it, else False."""
        schedules = self.get_schedules()
        if schedules:
            schedule['location'] = schedules[0].get('location') or {}
            if schedule == schedules[0]:
                logger.info('Schedule unchanged — skipping API call')
                return True

        # Never send a schedule without a location (E-003). Schedules are tied
        # to a location, so one without may not restrict charging at all.
        if not _valid_location(schedule['location']):
            if self.last_known_location is None:
                logger.error('Not writing charging schedule: no location from '
                             'Rivian and none seen since startup (E-003)')
                return False
            logger.warning('No location from Rivian — using last known %s',
                           self.last_known_location)
            schedule['location'] = dict(self.last_known_location)

        logger.info('Applying charging schedule: %s', schedule)
        resp = self._post(self.GATEWAY_URL, {
            "operationName": "SetChargingSchedule",
            "variables": {
                "vehicleId": self.vehicle_id,
                "chargingSchedules": [schedule],
            },
            "query": (
                "mutation SetChargingSchedule($vehicleId: String!, "
                "$chargingSchedules: [InputChargingSchedule!]!) { "
                "setChargingSchedules(vehicleId: $vehicleId, "
                "chargingSchedules: $chargingSchedules) { success } }"
            )
        })
        # GraphQL reports errors as HTTP 200 with data: null (C-002), so a 200
        # alone does not mean the write landed.
        ok = False
        if resp.status_code == 200:
            try:
                result = (resp.json().get('data') or {}).get('setChargingSchedules')
                ok = result is not None and result.get('success') is not False
            except (ValueError, AttributeError):
                ok = False
        if ok:
            logger.info('Charging schedule updated successfully')
        else:
            logger.error('SetChargingSchedule failed: %s', resp.text)
        return ok

    @_schedule_locked
    def set_charge_amps(self, amps):
        """
        Set charging rate.
          amps = 0        → disable charging via off-hours 1-hour window
          amps = 8..48    → charge at that rate via 24h schedule
        Returns True if the car now has the schedule, else False (CH-001).
        """
        amps = int(amps)

        if amps == 0:
            # Place a 1-hour window that never overlaps with current time:
            # before noon → schedule at 18:00, after noon → schedule at 06:00
            current_hour = datetime.now().hour
            start_min = 18 * 60 if current_hour < 12 else 6 * 60
            return self._apply_schedule({
                "startTime": start_min,
                "duration": 60,
                "location": {},
                "amperage": self.AMPS_PARK,
                "enabled": True,
                "weekDays": ["Monday", "Tuesday", "Wednesday", "Thursday",
                             "Friday", "Saturday", "Sunday"],
            })
        else:
            amps = max(self.AMPS_MIN, min(self.AMPS_MAX, amps))
            return self._apply_schedule({
                "startTime": 0,
                "duration": 1440,
                "location": {},
                "amperage": amps,
                "enabled": True,
                "weekDays": ["Monday", "Tuesday", "Wednesday", "Thursday",
                             "Friday", "Saturday", "Sunday"],
            })

    @_schedule_locked
    def enable_charging(self, enabled: bool):
        """Enable or disable charging. When enabling, EVCC will call setcurrent
        immediately after. Returns True if the car now has the schedule."""
        if enabled:
            current = self.get_current_amps()
            # Restore to at least AMPS_MIN so EVCC can ramp up
            return self.set_charge_amps(current if current >= self.AMPS_MIN else self.AMPS_MIN)
        return self.set_charge_amps(0)
