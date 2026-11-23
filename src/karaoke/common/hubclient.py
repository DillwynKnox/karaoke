"""Node side of the split setup: talking to the hub over Tailscale.

The player node (laptop) and the guest node (VPS) keep no session state of their own, they
forward to the hub and - on the player - fall back to the last state they saw when the hub
is briefly unreachable.
"""

import requests

from karaoke import config


class HubUnreachable(Exception):
    """The hub did not answer in time; the caller decides whether a cached answer will do."""


class HubClient:
    """Requests against one of the hub's APIs (admin, player or guest)."""

    def __init__(self, port):
        if not config.HUB_HOST:
            raise ValueError('HUB_HOST is not set')
        self.base = f'http://{config.HUB_HOST}:{port}'
        self._session = requests.Session()
        if config.HUB_PROXY:
            self._session.proxies = {'http': config.HUB_PROXY, 'https': config.HUB_PROXY}
            self._session.trust_env = False

    def _headers(self, extra=None):
        headers = dict(extra or {})
        if config.HUB_TOKEN:
            headers['Authorization'] = f'Bearer {config.HUB_TOKEN}'
        return headers

    def request(self, method, path, *, params=None, json=None, stream=False, timeout=None):
        try:
            resp = self._session.request(method, f'{self.base}{path}', params=params, json=json, stream=stream,
                                         headers=self._headers(), timeout=timeout or config.HUB_TIMEOUT)
        except requests.RequestException as e:
            raise HubUnreachable(str(e)) from e
        return resp

    def json(self, method, path, **kwargs):
        """Decoded body and status code of a hub call.

        Only a hub that cannot be reached at all raises; an answer the hub did give is
        passed on as it is. Its error pages are HTML, so those become a plain message
        under the status the hub chose - a node must not report a 404 or a refused token
        as 'hub offline' and hide what is really wrong."""
        resp = self.request(method, path, **kwargs)
        try:
            return resp.json(), resp.status_code
        except ValueError:
            if resp.status_code == 401:
                return {'error': 'Hub refused the token'}, 401
            return {'error': f'Hub answered {resp.status_code}'}, resp.status_code


def admin_client():
    return HubClient(config.HUB_ADMIN_PORT)


def player_client():
    return HubClient(config.HUB_PLAYER_PORT)


def guest_client():
    return HubClient(config.HUB_GUEST_PORT)
