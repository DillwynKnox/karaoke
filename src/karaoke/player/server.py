"""Player node: admin UI and player, running on the laptop next to the screen.

It keeps no session state of its own. Every API call is forwarded to the hub over
Tailscale, while the audio files and lyrics are served from the local cache that the
prefetcher fills - so a song that has already started keeps playing when the hub blinks.
"""

from flask import abort, render_template, request, send_file

from karaoke import config
from karaoke.common.hubclient import HubUnreachable, admin_client, player_client
from karaoke.common.invite import invite_url
from karaoke.common.web import check_key, guest_qr, make_app, serve, shown_host
from karaoke.player.cache import AssetCache, Prefetcher

admin_app = make_app('admin')
player_app = make_app('player')

_admin = None
_player = None
_cache = None
_prefetch = None


def _forward(client, path):
    """Passes the current request on to the hub and returns its JSON answer."""
    body = request.get_json(silent=True) if request.method in ('POST', 'PUT', 'PATCH') else None
    data, status = client.json(request.method, path, params=request.args, json=body)
    return data, status


def _decorate(state):
    """Marks the hub as reachable and points the UI at the public guest page."""
    if isinstance(state, dict) and 'queue' in state:
        state['hub_connected'] = True
        _add_guest_url(state)
    return state


def _add_guest_url(state):
    # Set here rather than on the hub: the laptop holds the invite secret and shows the QR.
    url = invite_url()
    if url:
        state['guest_url'] = url


def _offline_state():
    """Last state seen from the hub, so the UI keeps showing what is playing."""
    state, age = _prefetch.last_state()
    if not state:
        return {'error': 'Hub unreachable'}, 503
    offline = {**state, 'hub_connected': False, 'hub_state_age': round(age, 1)}
    _add_guest_url(offline)
    return offline, 200


# --- Admin UI ---

@admin_app.get('/')
def admin_index():
    return render_template('control.html', admin=True)


admin_app.add_url_rule('/api/guest-qr', 'guest_qr', guest_qr)


@admin_app.route('/api/<path:rest>', methods=['GET', 'POST', 'DELETE'])
def admin_proxy(rest):
    try:
        data, status = _forward(_admin, f'/api/{rest}')
    except HubUnreachable:
        return _offline_state() if rest == 'state' else ({'error': 'Hub unreachable'}, 503)
    _prefetch.remember(data)
    return _decorate(data), status


# --- Player UI ---

@player_app.get('/')
def player_index():
    return render_template('player.html')


player_app.add_url_rule('/api/guest-qr', 'guest_qr', guest_qr)


@player_app.post('/api/sync')
@player_app.post('/api/start')
@player_app.post('/api/ended')
def player_proxy():
    action = request.path.rsplit('/', 1)[-1]
    try:
        data, status = _forward(_player, f'/api/{action}')
    except HubUnreachable:
        return _offline_state()
    _prefetch.remember(data)
    return _decorate(data), status


@player_app.get('/api/lyrics/<key>')
def player_lyrics(key):
    check_key(key)
    fresh = _cache.fetch_lyrics(key)
    if fresh is not None:
        return fresh
    cached = _cache.get_lyrics(key)
    return cached if cached else ({'status': 'missing'}, 200)


@player_app.get('/storage/<path:rest>')
def player_asset(rest):
    """Audio from the local cache; anything not cached yet is pulled from the hub now."""
    url_path = f'/storage/{rest}'
    if not _cache.local_path(url_path):
        abort(404)
    path = _cache.ensure(url_path)
    if not path:
        # Not cached and the hub is not answering - the player shows its offline notice.
        abort(503)
    return send_file(path, conditional=True)


def start():
    global _admin, _player, _cache, _prefetch
    if not config.HUB_HOST:
        raise SystemExit('Player node needs HUB_HOST (the Tailscale name or IP of the hub)')

    _admin, _player = admin_client(), player_client()
    _cache = AssetCache(_player)
    _prefetch = Prefetcher(_cache, _admin)
    _prefetch.start()

    host = shown_host()
    print(f'Hub:            {_admin.base}')
    print(f'Cache:          {_cache.root}')
    print(f'Karaoke admin:  http://{host}:{config.ADMIN_PORT}')
    print(f'Karaoke player: http://{host}:{config.PLAYER_PORT}')

    serve(player_app, config.PLAYER_PORT, background=True)
    serve(admin_app, config.ADMIN_PORT)
