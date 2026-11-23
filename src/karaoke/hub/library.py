"""Metadata of every song that was ever queued, stored in storage/library.json."""

import json
import os
import threading
import time

from karaoke import config

_path = config.STORAGE_DIR / 'library.json'
_lock = threading.Lock()
_songs = None


def song_key(info):
    """Storage key of a song: its ISRC, or the Deezer id when Deezer has none."""
    return info.get('isrc') or f"deezer-{info['deezer_id']}"


def _load():
    global _songs
    if _songs is None:
        try:
            with open(_path) as f:
                _songs = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            _songs = {}
    return _songs


def get(key):
    with _lock:
        return _load().get(key)


def put(key, info):
    with _lock:
        songs = _load()
        songs[key] = {**info, 'added_at': songs.get(key, {}).get('added_at', time.time())}
        tmp = _path.with_suffix('.json.tmp')
        with open(tmp, 'w') as f:
            json.dump(songs, f, ensure_ascii=False)
        os.replace(tmp, _path)


def search(query, limit=5):
    words = query.lower().split()
    with _lock:
        songs = list(_load().values())
    matches = [s for s in songs if all(w in f"{s['title']} {s['artist']}".lower() for w in words)]
    matches.sort(key=lambda s: s.get('added_at', 0), reverse=True)
    return matches[:limit]
