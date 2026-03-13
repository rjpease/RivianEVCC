"""
Run this script once to authenticate with Rivian's 2-factor auth and
save the session tokens to rivian-session.json.

Usage:
    python RivianSessionInitOTP.py

Reads credentials from config.json and writes session to rivian-session.json
(both expected in the same directory as this script, or in rivian_bridge/ if
running inside the container).
"""

import json
import sys
import uuid
import requests

GATEWAY_URL = 'https://rivian.com/api/gql/gateway/graphql'

# Adjust paths if you run this from outside the rivian_bridge directory
CREDENTIALS_FILE = 'rivian_bridge/config.json'
SESSION_FILE = 'rivian_bridge/rivian-session.json'

# Run from the project root:  python RivianSessionInitOTP.py


def _headers(app_session_token=None, csrf_token=None):
    h = {
        'Content-Type': 'application/json',
        'User-Agent': 'RivianApp/1304 CFNetwork/1404.0.5 Darwin/22.3.0',
        'Accept': 'application/json',
        'Apollographql-Client-Name': 'com.rivian.ios.consumer-apollo-ios',
        'Dc-Cid': f'm-ios-{uuid.uuid4()}',
    }
    if app_session_token:
        h['a-sess'] = app_session_token
    if csrf_token:
        h['csrf-token'] = csrf_token
    return h


def main():
    with open(CREDENTIALS_FILE) as f:
        config = json.load(f)
    username = config['rivian-user']
    password = config['rivian-pass']

    # Step 1 — CSRF token
    print('Requesting CSRF token...')
    resp = requests.post(GATEWAY_URL, headers=_headers(), json={
        'operationName': 'CreateCSRFToken',
        'variables': [],
        'query': 'mutation CreateCSRFToken { createCsrfToken { __typename csrfToken appSessionToken } }',
    })
    resp.raise_for_status()
    data = resp.json()
    csrf_token = data['data']['createCsrfToken']['csrfToken']
    app_session_token = data['data']['createCsrfToken']['appSessionToken']

    # Step 2 — Login (triggers OTP)
    print('Logging in (OTP will be sent to your email/phone)...')
    resp = requests.post(GATEWAY_URL, headers=_headers(app_session_token, csrf_token), json={
        'operationName': 'Login',
        'variables': {'email': username, 'password': password},
        'query': (
            'mutation Login($email: String!, $password: String!) { '
            'login(email: $email, password: $password) { '
            '__typename '
            '... on MobileLoginResponse { __typename accessToken refreshToken userSessionToken } '
            '... on MobileMFALoginResponse { __typename otpToken } } }'
        ),
    })
    resp.raise_for_status()
    data = resp.json()
    otp_token = data['data']['login']['otpToken']

    # Step 3 — OTP verification
    otp_code = input('Enter the authentication code sent to your email/phone: ').strip()

    resp = requests.post(GATEWAY_URL, headers=_headers(app_session_token, csrf_token), json={
        'operationName': 'LoginWithOTP',
        'variables': {
            'email': username,
            'otpCode': otp_code,
            'otpToken': otp_token,
        },
        'query': (
            'mutation LoginWithOTP($email: String!, $otpCode: String!, $otpToken: String!) { '
            'loginWithOTP(email: $email, otpCode: $otpCode, otpToken: $otpToken) { '
            '__typename accessToken refreshToken userSessionToken } }'
        ),
    })
    resp.raise_for_status()
    data = resp.json()
    user_session_token = data['data']['loginWithOTP']['userSessionToken']

    # Step 4 — Persist session
    session = {
        'appSessionToken': app_session_token,
        'userSessionToken': user_session_token,
        'csrfToken': csrf_token,
    }
    with open(SESSION_FILE, 'w') as f:
        json.dump(session, f, indent=2)

    print(f'Session saved to {SESSION_FILE}')


if __name__ == '__main__':
    main()
