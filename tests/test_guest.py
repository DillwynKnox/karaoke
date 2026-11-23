"""The public guest page: nothing without a valid invite code."""

import pytest
from itsdangerous import URLSafeTimedSerializer

from karaoke import config
from karaoke.common import invite
from karaoke.guest import server


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, 'GUEST_INVITE_SECRET', 'secret')
    monkeypatch.setattr(config, 'GUEST_ACCESS_CODE', '')
    monkeypatch.setattr(server, '_signer', URLSafeTimedSerializer('cookie-secret', salt='karaoke-guest'))
    return server.app.test_client()


def test_locked_without_cookie(client):
    assert client.get('/').status_code == 403
    assert client.get('/api/state').status_code == 401


def test_wrong_code_is_refused(client):
    assert client.get('/k/wrongcode1').status_code == 403


def test_valid_code_hands_out_cookie(client):
    resp = client.get(f'/k/{invite.current_code()}')
    assert resp.status_code == 302
    assert server.COOKIE_NAME in resp.headers['Set-Cookie']
    assert client.get('/').status_code == 200
