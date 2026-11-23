"""Song search via the public Deezer API (no account or API key needed)."""

import functools

import requests

DEEZER_URL = 'https://api.deezer.com'


class DeezerError(Exception):
    pass


def _get(path, params=None):
    resp = requests.get(f'{DEEZER_URL}/{path}', params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if 'error' in data:
        raise DeezerError(data['error'].get('message', 'unknown error'))
    return data


def _song(track):
    return {
        'deezer_id': track['id'],
        'title': track['title'],
        'title_short': track.get('title_short') or track['title'],
        'artist': track['artist']['name'],
        'album': track['album']['title'],
        'duration': track['duration'],
        'isrc': track.get('isrc') or None,
        'cover': track['album'].get('cover_medium'),
        'cover_small': track['album'].get('cover_small'),
    }


@functools.lru_cache(maxsize=256)
def _search(query, limit):
    return tuple(_song(t) for t in _get('search', {'q': query, 'limit': limit})['data'] if t.get('type') == 'track')


def search(query, limit=8):
    return [dict(song) for song in _search(query.strip().lower(), limit)]


def track(deezer_id):
    return _song(_get(f'track/{int(deezer_id)}'))
