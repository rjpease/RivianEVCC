# Rivian API — vehicle state reader + charging schedule controller for EVCC bridge
# Adapted from github.com/ostap-korkuna/rivian-charging-automation

import json
import logging
import os
import requests
from datetime import datetime

logger = logging.getLogger(__name__)


class RivianAPI:
    GATEWAY_URL = 'https://rivian.com/api/gql/gateway/graphql'

    AMPS_MAX = 48
    AMPS_MIN = 8

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

        # Cached vehicle state (populated by refresh())
        self.charging_status = None
        self.battery_level = None
        self.distance_to_empty_mi = None

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
        return requests.post(url, headers=self._headers(), json=request)

    def init_session(self):
        logger.info('Initializing new Rivian session...')
        resp = requests.post(
            self.GATEWAY_URL,
            headers={'Content-Type': 'application/json'},
            json={
                "operationName": "CreateCSRFToken",
                "variables": [],
                "query": "mutation CreateCSRFToken { createCsrfToken { __typename csrfToken appSessionToken } }"
            }
        )
        if resp.status_code != 200:
            logger.error('CSRF request failed: %s', resp.text)
            return False

        data = resp.json()
        self.csrf_token = data['data']['createCsrfToken']['csrfToken']
        self.app_session_token = data['data']['createCsrfToken']['appSessionToken']

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
            logger.error('Login failed: %s', resp.text)
            return False

        data = resp.json()
        self.user_session_token = data['data']['login']['userSessionToken']
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
        except (KeyError, IndexError) as e:
            logger.error('Failed to parse vehicle ID: %s', e)
            return False

    def login(self):
        if os.path.exists(self.session_file):
            logger.info('Loading stored session from %s', self.session_file)
            with open(self.session_file) as f:
                d = json.load(f)
                self.app_session_token = d['appSessionToken']
                self.user_session_token = d['userSessionToken']
                self.csrf_token = d['csrfToken']

        if not self.init_user_info():
            logger.info('Stored session invalid — re-authenticating')
            if self.init_session() and self.init_user_info():
                self._persist_session()
            else:
                logger.error('Rivian auth failed. Run RivianSessionInitOTP.py')

    def _persist_session(self):
        with open(self.session_file, 'w') as f:
            json.dump({
                'appSessionToken': self.app_session_token,
                'userSessionToken': self.user_session_token,
                'csrfToken': self.csrf_token,
            }, f)
        logger.info('Session persisted to %s', self.session_file)

    # ------------------------------------------------------------------
    # Vehicle state (EVCC vehicle plugin)
    # ------------------------------------------------------------------

    def refresh(self):
        """Fetch the latest vehicle state from Rivian cloud. Updates cached fields."""
        if not self.vehicle_id:
            logger.error('No vehicle ID — cannot refresh')
            return

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
            return

        state = resp.json().get('data', {}).get('vehicleState', {})
        if state.get('chargerStatus') is None:
            logger.warning('Vehicle state missing — possibly in service mode')
            return

        self.charging_status = state['chargerStatus']['value']
        self.battery_level = state['batteryLevel']['value']

        dte = state.get('distanceToEmpty')
        if dte:
            self.distance_to_empty_mi = round(dte['value'] / 1.60934)

        logger.info('Refreshed: status=%s soc=%.1f%% range=%s mi',
                    self.charging_status, self.battery_level, self.distance_to_empty_mi)

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

    def get_range_mi(self):
        return self.distance_to_empty_mi

    def is_charging(self):
        return self.charging_status == self.STATUS_CONNECTED_CHARGING

    # ------------------------------------------------------------------
    # Charging schedule control (EVCC charger plugin)
    # ------------------------------------------------------------------

    def get_schedules(self):
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
        return resp.json()['data']['getVehicle']['chargingSchedules']

    def get_current_amps(self):
        """Return active schedule amperage, or 0 if charging is disabled."""
        schedules = self.get_schedules()
        if not schedules:
            return 0
        s = schedules[0]
        # A 24-hour schedule (duration=1440) means charging is enabled
        if s.get('duration', 0) == 1440 and s.get('enabled', False):
            return s.get('amperage', 0)
        return 0

    def _apply_schedule(self, schedule):
        schedules = self.get_schedules()
        if schedules:
            schedule['location'] = schedules[0].get('location', {})
            if schedule == schedules[0]:
                logger.info('Schedule unchanged — skipping API call')
                return

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
        if resp.status_code == 200:
            logger.info('Charging schedule updated successfully')
        else:
            logger.error('SetChargingSchedule failed: %s', resp.text)

    def set_charge_amps(self, amps):
        """
        Set charging rate.
          amps = 0        → disable charging via off-hours 1-hour window
          amps = 8..48    → charge at that rate via 24h schedule
        """
        amps = int(amps)

        if amps == 0:
            # Place a 1-hour window that never overlaps with current time:
            # before noon → schedule at 18:00, after noon → schedule at 06:00
            current_hour = datetime.now().hour
            start_min = 18 * 60 if current_hour < 12 else 6 * 60
            self._apply_schedule({
                "startTime": start_min,
                "duration": 60,
                "location": {},
                "amperage": self.AMPS_MAX,
                "enabled": True,
                "weekDays": ["Monday", "Tuesday", "Wednesday", "Thursday",
                             "Friday", "Saturday", "Sunday"],
            })
        else:
            amps = max(self.AMPS_MIN, min(self.AMPS_MAX, amps))
            self._apply_schedule({
                "startTime": 0,
                "duration": 1440,
                "location": {},
                "amperage": amps,
                "enabled": True,
                "weekDays": ["Monday", "Tuesday", "Wednesday", "Thursday",
                             "Friday", "Saturday", "Sunday"],
            })

    def enable_charging(self, enabled: bool):
        """Enable or disable charging. When enabling, EVCC will call setcurrent immediately after."""
        if enabled:
            current = self.get_current_amps()
            # Restore to at least AMPS_MIN so EVCC can ramp up
            self.set_charge_amps(current if current >= self.AMPS_MIN else self.AMPS_MIN)
        else:
            self.set_charge_amps(0)
