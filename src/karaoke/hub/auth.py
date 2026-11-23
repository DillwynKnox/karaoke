"""Hub side of the split setup: only localhost and nodes that know HUB_TOKEN get in.

Transport security is Tailscale's job - the token only makes sure that a process which
somehow reaches the port still has to be one of our own nodes.
"""

import hmac

from flask import abort, request

from karaoke import config

LOCAL_ADDRESSES = ('127.0.0.1', '::1')


def _presented_token():
    header = request.headers.get('Authorization', '')
    if header.startswith('Bearer '):
        return header[len('Bearer '):]
    return request.headers.get('X-Hub-Token', '')


def _check():
    # The UIs served by the hub itself are opened locally, without a token.
    if not config.HUB_TOKEN or request.remote_addr in LOCAL_ADDRESSES:
        return
    if not hmac.compare_digest(_presented_token(), config.HUB_TOKEN):
        abort(401)


def protect(*apps):
    """Requires the hub token on every request of these apps that comes from another host."""
    for app in apps:
        app.before_request(_check)
