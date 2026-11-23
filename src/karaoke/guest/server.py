"""Guest node: the one public page, made to run in Docker on a VPS.

It serves the guest UI and forwards search and queue calls to the hub over Tailscale.
Nothing else of the karaoke is reachable from the internet - no playback control, no audio,
no admin API. Guests get in through a link that carries an invite code - the rotating one
from the QR on the player screen, or the fixed GUEST_ACCESS_CODE; from then on a signed
cookie identifies them, which is what the per-guest limits are counted against.
"""

import hmac
import threading
import time
import uuid

from flask import make_response, redirect, render_template, request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.middleware.proxy_fix import ProxyFix

from karaoke import config
from karaoke.common import invite
from karaoke.common.hubclient import HubUnreachable, guest_client
from karaoke.common.web import make_app, serve
from karaoke.guest.ratelimit import RateLimiter

COOKIE_NAME = 'karaoke_guest'
MAX_QUERY_LENGTH = 120
# Bodies are tiny JSON objects; anything larger is not one of our own requests.
MAX_BODY_BYTES = 4096
# The queue is asked for at most this often when counting a guest's open songs.
STATE_TTL = 2.0

app = make_app('guest')
app.config['MAX_CONTENT_LENGTH'] = MAX_BODY_BYTES

_hub = None
_limiter = RateLimiter()
_signer = None
# guest id -> ids of the items that guest queued, oldest first.
_queued_by_guest = {}
_queued_lock = threading.Lock()
_state_cache = {'at': 0.0, 'state': None}
_state_lock = threading.Lock()


# --- Guest identity ---

def _guest_id():
    """Id from the signed cookie, or None when there is no valid one."""
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return None
    try:
        return _signer.loads(cookie, max_age=config.GUEST_COOKIE_DAYS * 86400)
    except BadSignature:
        return None


def _client_ip():
    return request.remote_addr or 'unknown'


def _limit_keys(guest_id):
    return (f'guest:{guest_id}', f'ip:{_client_ip()}')


def _code_accepted(code):
    if invite.is_valid(code):
        return True
    return bool(config.GUEST_ACCESS_CODE) and hmac.compare_digest(code, config.GUEST_ACCESS_CODE)


@app.get('/k/<code>')
def enter(code):
    """The link behind the QR code. A valid code hands out a guest cookie."""
    if not _code_accepted(code):
        # A code from an old photo of the screen: a cookie that is still valid keeps working.
        if _guest_id():
            return redirect('/')
        return render_template('guest_locked.html', expired=bool(config.GUEST_INVITE_SECRET)), 403
    guest_id = _guest_id() or uuid.uuid4().hex
    response = make_response(redirect('/'))
    response.set_cookie(COOKIE_NAME, _signer.dumps(guest_id), max_age=config.GUEST_COOKIE_DAYS * 86400,
                        httponly=True, samesite='Lax', secure=request.is_secure)
    return response


@app.before_request
def require_guest():
    if request.endpoint in ('enter', 'static'):
        return None
    if _guest_id():
        return None
    if request.path.startswith('/api/'):
        return {'error': 'Not signed in'}, 401
    return render_template('guest_locked.html'), 403


# --- Hub calls ---

def _state(max_age=0.0):
    """Current hub state, reused for up to max_age seconds."""
    with _state_lock:
        cached = _state_cache
    if max_age and cached['state'] is not None and time.time() - cached['at'] < max_age:
        return cached['state']

    state, status = _hub.json('GET', '/api/state')
    if status != 200:
        raise HubUnreachable(f'state answered {status}')
    with _state_lock:
        _state_cache.update(at=time.time(), state=state)
    return state


@app.get('/')
def index():
    return render_template('control.html', admin=False)


@app.get('/api/state')
def api_state():
    guest_id = _guest_id()
    if not _limiter.allow('state', _limit_keys(guest_id), config.GUEST_STATE_PER_MIN, 60):
        return {'error': 'Slow down'}, 429
    try:
        return _state(max_age=1.0)
    except HubUnreachable:
        return {'error': 'Karaoke is offline'}, 503


@app.get('/api/search')
def api_search():
    guest_id = _guest_id()
    if not _limiter.allow('search', _limit_keys(guest_id), config.GUEST_SEARCH_PER_MIN, 60):
        return {'error': 'Too many searches, wait a moment'}, 429
    query = request.args.get('q', '')[:MAX_QUERY_LENGTH]
    try:
        data, status = _hub.json('GET', '/api/search', params={'q': query})
    except HubUnreachable:
        return {'error': 'Search is offline'}, 503
    return data, status


@app.post('/api/queue')
def api_queue():
    guest_id = _guest_id()
    open_songs = _open_songs(guest_id)
    if open_songs is None:
        return {'error': 'Karaoke is offline'}, 503
    if open_songs >= config.GUEST_MAX_OPEN:
        return {'error': f'Only {config.GUEST_MAX_OPEN} songs in the queue per person. '
                         f'Add the next one once one of yours has been sung.'}, 429
    if not _limiter.allow('add', _limit_keys(guest_id), config.GUEST_ADD_PER_MIN, 60) or \
            not _limiter.allow('add_hour', _limit_keys(guest_id), config.GUEST_ADD_PER_HOUR, 3600):
        return {'error': 'Too many songs at once, wait a moment'}, 429

    body = request.get_json(silent=True) or {}
    try:
        data, status = _hub.json('POST', '/api/queue', json={k: body.get(k) for k in
                                                             ('deezer_id', 'mode', 'instrumental_db', 'vocals_db',
                                                              'original_db')})
    except HubUnreachable:
        return {'error': 'Karaoke is offline'}, 503

    if status == 200 and isinstance(data, dict) and data.get('id'):
        _remember(guest_id, data['id'])
        # The queue just changed, so the next state call fetches a fresh one.
        with _state_lock:
            _state_cache['at'] = 0.0
    return data, status


def _remember(guest_id, item_id):
    with _queued_lock:
        items = _queued_by_guest.setdefault(guest_id, [])
        items.append(item_id)
        # Only the open ones matter; the tail is pruned when it is counted.
        del items[:-200]


def _open_songs(guest_id):
    """How many of this guest's songs are still waiting or playing, None if the hub is down."""
    with _queued_lock:
        mine = list(_queued_by_guest.get(guest_id, []))
    if not mine:
        return 0
    try:
        state = _state(max_age=STATE_TTL)
    except HubUnreachable:
        return None

    live = {item['id'] for item in [state.get('current'), *state.get('queue', [])] if item}
    still_open = [item_id for item_id in mine if item_id in live]
    with _queued_lock:
        _queued_by_guest[guest_id] = still_open
    return len(still_open)


def start():
    global _hub, _signer
    if not config.HUB_HOST:
        raise SystemExit('Guest node needs HUB_HOST (the Tailscale name or IP of the hub)')
    if not config.GUEST_ACCESS_CODE and not config.GUEST_INVITE_SECRET:
        raise SystemExit('Guest node needs GUEST_INVITE_SECRET (rotating code of the QR on the player) '
                         'or GUEST_ACCESS_CODE (fixed code in the invite link)')
    if not config.GUEST_COOKIE_SECRET:
        raise SystemExit('Guest node needs GUEST_COOKIE_SECRET (any long random string)')

    _hub = guest_client()
    _signer = URLSafeTimedSerializer(config.GUEST_COOKIE_SECRET, salt='karaoke-guest')
    if config.GUEST_PROXY_HOPS:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=config.GUEST_PROXY_HOPS, x_proto=config.GUEST_PROXY_HOPS,
                                x_host=config.GUEST_PROXY_HOPS)

    print(f'Hub:            {_hub.base}')
    if config.GUEST_INVITE_SECRET:
        rotation = f'rotating every {config.GUEST_CODE_MINUTES} min' if config.GUEST_CODE_MINUTES else 'fixed'
        print(f'Invite code:    {rotation}, now http://{config.HTTP_HOST}:{config.GUEST_PORT}/k/{invite.current_code()}')
    if config.GUEST_ACCESS_CODE:
        print(f'Guest page:     http://{config.HTTP_HOST}:{config.GUEST_PORT}/k/{config.GUEST_ACCESS_CODE}')
    print(f'Limits:         {config.GUEST_MAX_OPEN} open songs per guest, {config.GUEST_ADD_PER_MIN}/min and '
          f'{config.GUEST_ADD_PER_HOUR}/h added, {config.GUEST_SEARCH_PER_MIN} searches/min')
    serve(app, config.GUEST_PORT)
