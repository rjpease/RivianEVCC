"""Meta-tests: prove the suite cannot reach the real Rivian API.

This project writes charging schedules to a real vehicle. If the guard in
conftest ever stops working, a test could quietly start driving a car, so the
guard itself is tested.
"""

import pytest
import requests

from conftest import RealNetworkAccessError


@pytest.mark.parametrize('method', ['post', 'get', 'put', 'patch', 'delete',
                                    'request'])
def test_unstubbed_requests_are_blocked(method):
    with pytest.raises(RealNetworkAccessError):
        getattr(requests, method)('https://rivian.com/api/gql/gateway/graphql')


def test_sessions_are_blocked_too():
    with pytest.raises(RealNetworkAccessError):
        requests.Session().get('https://rivian.com/api/gql/gateway/graphql')


def test_the_gateway_fixture_overrides_the_guard(gateway):
    """The stub must be reachable where a test asks for it, so the guard is not
    silently making every gateway test vacuous."""
    import rivian_api

    response = rivian_api.requests.post(
        'https://rivian.com/api/gql/gateway/graphql',
        headers={}, json={'operationName': 'GetChargingSchedule'})

    assert response.status_code == 200
    assert gateway.operations() == ['GetChargingSchedule']


def test_api_fixture_never_reaches_the_network(api, gateway, at_clock):
    """A full round trip through the real code paths stays on the stub."""
    import rivian_api

    at_clock(rivian_api, hour=9)
    api.refresh()
    api.set_charge_amps(16)

    assert all(call['url'] == rivian_api.RivianAPI.GATEWAY_URL
               for call in gateway.calls)
