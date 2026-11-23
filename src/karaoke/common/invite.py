"""Invite codes that change every few minutes, shown as a QR code on the player screen.

The machine that shows the QR (player node or hub) and the guest node share
GUEST_INVITE_SECRET and derive the same code from it and the clock, so they never have to
talk to each other about it. A photo of the screen is only good for a short while: the guest
node accepts the current code and its direct neighbours, which also covers a bit of clock skew
and someone scanning just before the code changes. Only entering needs a fresh code: whoever
got in once is recognised by the guest cookie from then on. GUEST_CODE_MINUTES=0 keeps the
code fixed.
"""

import base64
import hashlib
import hmac
import time

from karaoke import config

CODE_LENGTH = 10


def _window():
    if not config.GUEST_CODE_MINUTES:
        return 0
    return int(time.time() // (config.GUEST_CODE_MINUTES * 60))


def _code_for(window):
    digest = hmac.new(config.GUEST_INVITE_SECRET.encode(), f'invite:{window}'.encode(), hashlib.sha256).digest()
    return base64.b32encode(digest).decode().lower()[:CODE_LENGTH]


def current_code():
    return _code_for(_window())


def is_valid(code):
    if not config.GUEST_INVITE_SECRET or len(code) != CODE_LENGTH:
        return False
    window = _window()
    return any(hmac.compare_digest(code, _code_for(window + delta)) for delta in (-1, 0, 1))


def invite_url():
    """Link for the QR code: with a rotating code when there is a secret, else PUBLIC_GUEST_URL as it is."""
    if not config.PUBLIC_GUEST_URL or not config.GUEST_INVITE_SECRET:
        return config.PUBLIC_GUEST_URL
    return f'{config.PUBLIC_GUEST_URL.rstrip("/")}/k/{current_code()}'
