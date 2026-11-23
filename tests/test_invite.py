import time

from karaoke import config
from karaoke.common import invite


def test_current_code_is_valid(monkeypatch):
    monkeypatch.setattr(config, 'GUEST_INVITE_SECRET', 'secret')
    monkeypatch.setattr(config, 'GUEST_CODE_MINUTES', 10)
    assert invite.is_valid(invite.current_code())
    assert not invite.is_valid('x' * invite.CODE_LENGTH)


def test_code_expires(monkeypatch):
    monkeypatch.setattr(config, 'GUEST_INVITE_SECRET', 'secret')
    monkeypatch.setattr(config, 'GUEST_CODE_MINUTES', 10)
    code = invite.current_code()
    now = time.time()
    monkeypatch.setattr(time, 'time', lambda: now + 30 * 60)
    assert not invite.is_valid(code)


def test_no_secret_no_codes(monkeypatch):
    monkeypatch.setattr(config, 'GUEST_INVITE_SECRET', '')
    assert not invite.is_valid('abcdefghij')


def test_invite_url(monkeypatch):
    monkeypatch.setattr(config, 'PUBLIC_GUEST_URL', 'https://karaoke.example.com/')
    monkeypatch.setattr(config, 'GUEST_INVITE_SECRET', 'secret')
    assert invite.invite_url() == f'https://karaoke.example.com/k/{invite.current_code()}'
