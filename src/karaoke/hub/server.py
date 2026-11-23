"""The hub, serving three separate UIs.

- admin: search, queue management, playback control and the live mix
- guest: search and queue songs with a mode, no control over what is playing
- player: plays the tracks and shows the lyrics

All three run in this process and share one KaraokeSession. In the split setup the same
three APIs are what the player node (laptop) and the guest node (VPS) talk to over
Tailscale; HUB_TOKEN then guards every request that does not come from this machine.
"""

from concurrent.futures import ThreadPoolExecutor, wait

import requests
from flask import abort, render_template, request, send_from_directory

from karaoke import config
from karaoke.common.invite import invite_url
from karaoke.common.web import check_key, guest_qr, make_app, serve, shown_host
from karaoke.hub import auth, deezer, library, lyrics
from karaoke.hub.mix import LEVELS
from karaoke.hub.session import KaraokeSession
from karaoke.hub.song import Song

# The lyrics check of search results may add at most this much to a search.
LYRICS_MATCH_TIMEOUT = 2.0
# Order of results of the same song: versions with fitting synced lyrics first.
_MATCH_RANK = {'synced': 0, 'other_version': 1, 'plain': 1, None: 1, 'none': 2}
_lookups = ThreadPoolExecutor(max_workers=8)

admin_app = make_app('admin')
guest_app = make_app('guest')
player_app = make_app('player')
session: KaraokeSession = None


def _state():
    state = {**session.state(), 'admin_port': config.ADMIN_PORT, 'guest_port': config.GUEST_PORT,
             'player_port': config.PLAYER_PORT, 'hub_connected': True}
    guest_url = invite_url()
    if guest_url:
        state['guest_url'] = guest_url
    return state


def _json_body():
    return request.get_json(silent=True) or {}


# --- Admin and guest UI ---

def _search():
    query = request.args.get('q', '').strip()
    if len(query) < 2:
        return {'results': []}

    error = None
    try:
        remote = deezer.search(query)
    except (requests.RequestException, deezer.DeezerError) as e:
        remote, error = [], f'Search failed: {e}'

    results, seen = [], set()
    for song in library.search(query) + remote:
        key = library.song_key(song)
        if key not in seen:
            seen.add(key)
            results.append({**song, 'key': key, 'ready': Song(key).is_prepared()})
    results = results[:10]
    _add_lyrics_match(results)
    return {'results': results, 'error': error}


def _add_lyrics_match(results):
    """Sets 'lyrics_match' (see lyrics.lyrics_match, None if the lookup failed or was too slow)
    and moves the versions of a song with fitting lyrics before its other versions."""
    def song_id(song):
        return lyrics.clean_title(song.get('title_short') or song['title']), song['artist']

    # One LRCLIB lookup per song; its versions are then checked against the cached result.
    lookups = {key: _lookups.submit(lyrics.search_lyrics, *key) for key in {song_id(song) for song in results}}
    wait(lookups.values(), timeout=LYRICS_MATCH_TIMEOUT)
    for song in results:
        lookup = lookups[song_id(song)]
        song['lyrics_match'] = lyrics.lyrics_match(song['title'], song['duration'], lookup.result()) \
            if lookup.done() and not lookup.exception() else None

    first_seen = {}
    for i, song in enumerate(results):
        first_seen.setdefault(song_id(song), i)
    results.sort(key=lambda song: (first_seen[song_id(song)], _MATCH_RANK[song['lyrics_match']]))


def _add():
    body = _json_body()
    try:
        deezer_id = int(body.get('deezer_id'))
    except (TypeError, ValueError):
        return {'error': 'deezer_id missing'}, 400
    try:
        return session.add(deezer_id, {name: body.get(name) for name in ('mode', *LEVELS)})
    except (requests.RequestException, deezer.DeezerError) as e:
        return {'error': f'Deezer lookup failed: {e}'}, 502
    except ValueError as e:
        return {'error': str(e)}, 400


for _app, _is_admin in ((admin_app, True), (guest_app, False)):
    _app.add_url_rule('/', 'index', lambda admin=_is_admin: render_template('control.html', admin=admin))
    _app.add_url_rule('/api/state', 'state', _state)
    _app.add_url_rule('/api/search', 'search', _search)
    _app.add_url_rule('/api/queue', 'add', _add, methods=['POST'])

admin_app.add_url_rule('/api/guest-qr', 'guest_qr', guest_qr)


# --- Admin only ---

@admin_app.delete('/api/queue/<item_id>')
def admin_remove(item_id):
    session.remove(item_id)
    return _state()


@admin_app.post('/api/queue/<item_id>/next')
def admin_play_next(item_id):
    session.play_next(item_id)
    return _state()


@admin_app.post('/api/player/<action>')
def admin_command(action):
    try:
        session.command(action)
    except ValueError:
        abort(404)
    return _state()


@admin_app.post('/api/settings')
def admin_settings():
    try:
        session.update_settings(_json_body())
    except (TypeError, ValueError):
        return {'error': 'invalid settings'}, 400
    return _state()


@admin_app.get('/api/lyrics/<key>')
def admin_get_lyrics(key):
    check_key(key)
    return lyrics.load_lyrics(key) or {'status': 'missing', 'lines': []}


@admin_app.post('/api/lyrics/<key>')
def admin_set_lyrics(key):
    check_key(key)
    text = _json_body().get('text')
    if not isinstance(text, str):
        return {'error': 'text missing'}, 400
    session.set_lyrics(key, text)
    return _state()


@admin_app.post('/api/mix')
def admin_mix():
    try:
        session.update_mix(_json_body())
    except (TypeError, ValueError) as e:
        return {'error': str(e)}, 400
    return _state()


@admin_app.post('/api/presets/<mode>')
def admin_preset(mode):
    try:
        session.update_preset(mode, _json_body())
    except (TypeError, ValueError) as e:
        return {'error': str(e)}, 400
    return _state()


# --- Player UI ---

@player_app.get('/')
def player_index():
    return render_template('player.html')


player_app.add_url_rule('/api/guest-qr', 'guest_qr', guest_qr)


@player_app.post('/api/sync')
def player_sync():
    try:
        session.player_sync(_json_body())
    except (TypeError, ValueError):
        return {'error': 'invalid report'}, 400
    return _state()


@player_app.post('/api/start')
def player_start():
    session.player_start(_json_body().get('item_id'))
    return _state()


@player_app.post('/api/ended')
def player_ended():
    session.player_ended(_json_body().get('item_id'))
    return _state()


@player_app.get('/api/lyrics/<key>')
def player_lyrics(key):
    check_key(key)
    song = library.get(key) or {}
    data = lyrics.ensure_lyrics(key, song.get('title_short'), song.get('artist'), song.get('album'),
                                song.get('duration'), Song(key).vocals_path)
    if not data:
        return {'status': 'missing'}
    return {**data, 'aligning': lyrics.is_aligning(key)}


@player_app.get('/storage/tracks/<key>.mp3')
def player_original(key):
    check_key(key)
    return send_from_directory(config.TRACKS_DIR, f'{key}.mp3')


@player_app.get('/storage/tracks/separated/htdemucs/<key>/<stem>')
def player_stem(key, stem):
    check_key(key)
    if stem not in ('vocals.mp3', 'no_vocals.mp3'):
        abort(404)
    return send_from_directory(config.STEMS_DIR / key, stem)


def start():
    global session
    import essentia  # noqa: F401  (loads its native libraries before the worker threads start)

    config.TRACKS_DIR.mkdir(parents=True, exist_ok=True)
    session = KaraokeSession()
    auth.protect(admin_app, guest_app, player_app)

    host = shown_host()
    print(f'Storage:        {config.STORAGE_DIR}')
    print(f'Karaoke admin:  http://{host}:{config.ADMIN_PORT}')
    print(f'Karaoke guests: http://{host}:{config.GUEST_PORT}')
    print(f'Karaoke player: http://{host}:{config.PLAYER_PORT}')
    if config.HUB_TOKEN:
        print('Hub token:      set (remote nodes must send it)')

    serve(guest_app, config.GUEST_PORT, background=True)
    serve(player_app, config.PLAYER_PORT, background=True)
    serve(admin_app, config.ADMIN_PORT)
