"""Local song cache of the player node.

The laptop plays from its own disk, not from the hub: as soon as the hub has finished a
song, its stems, the original and the lyrics are copied over. Songs already on disk keep
playing when the hub or Tailscale drops out for a moment.
"""

import json
import os
import threading
import time

from karaoke import config
from karaoke.common.hubclient import HubUnreachable

# Assets of an item, in the order the player needs them.
ASSET_FIELDS = ('no_vocals', 'vocals', 'original')
POLL_INTERVAL = 2.0
# Chunk size of a streamed download.
CHUNK = 1 << 16


def _safe_join(root, url_path):
    """Local path for a hub URL path, refusing anything that escapes the cache."""
    path = os.path.normpath(os.path.join(root, url_path.lstrip('/')))
    if path != root and not path.startswith(root + os.sep):
        return None
    return path


class AssetCache:
    """Files of the hub, mirrored under config.CACHE_DIR with the same paths."""

    def __init__(self, client):
        self._client = client
        self._root = os.path.abspath(config.CACHE_DIR)
        self._locks = {}
        self._locks_guard = threading.Lock()
        os.makedirs(self._root, exist_ok=True)

    @property
    def root(self):
        return self._root

    def _lock_for(self, url_path):
        """One lock per asset so a prefetch and a player request never download it twice."""
        with self._locks_guard:
            return self._locks.setdefault(url_path, threading.Lock())

    def local_path(self, url_path):
        return _safe_join(self._root, url_path)

    def has(self, url_path):
        path = self.local_path(url_path)
        return bool(path) and os.path.isfile(path)

    def ensure(self, url_path):
        """Local path of an asset, fetching it from the hub if it is not cached yet.
        Returns None when it is neither cached nor reachable."""
        path = self.local_path(url_path)
        if not path:
            return None
        if os.path.isfile(path):
            os.utime(path, None)  # keeps it young for the LRU eviction
            return path

        with self._lock_for(url_path):
            if os.path.isfile(path):
                return path
            try:
                return self._download(url_path, path)
            except (HubUnreachable, OSError) as e:
                print(f'Cache: {url_path} not available ({e})')
                return None

    def _download(self, url_path, path):
        resp = self._client.request('GET', url_path, stream=True, timeout=300)
        if resp.status_code != 200:
            resp.close()
            raise HubUnreachable(f'{url_path} answered {resp.status_code}')

        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f'{path}.part'
        try:
            with open(tmp, 'wb') as f:
                for chunk in resp.iter_content(CHUNK):
                    f.write(chunk)
            # Only a fully written file becomes visible under its real name.
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        finally:
            resp.close()
        return path

    # --- Lyrics ---

    def _lyrics_path(self, key):
        return os.path.join(self._root, 'lyrics', f'{key}.json')

    def put_lyrics(self, key, data):
        """Keeps the finished lyrics of a song; ones still being aligned are not cached."""
        if not data or data.get('status') == 'missing' or data.get('aligning'):
            return
        path = self._lyrics_path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f'{path}.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)

    def get_lyrics(self, key):
        try:
            with open(self._lyrics_path(key)) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def fetch_lyrics(self, key):
        """Lyrics of a song from the hub, cached for as long as they stay unchanged."""
        try:
            data, status = self._client.json('GET', f'/api/lyrics/{key}')
        except HubUnreachable:
            return None
        if status != 200:
            return None
        self.put_lyrics(key, data)
        return data

    # --- Eviction ---

    def size_bytes(self):
        total = 0
        for root, _, files in os.walk(self._root):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return total

    def evict(self, keep_paths):
        """Drops the oldest cached files until the cache fits into CACHE_MAX_GB again.
        Files of the songs in keep_paths (current and prefetched) are never dropped."""
        limit = int(config.CACHE_MAX_GB * (1 << 30))
        if limit <= 0:
            return
        entries = []
        for root, _, files in os.walk(self._root):
            for name in files:
                path = os.path.join(root, name)
                if path in keep_paths:
                    continue
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                entries.append((stat.st_atime, stat.st_size, path))

        total = self.size_bytes()
        for _, size, path in sorted(entries):
            if total <= limit:
                return
            try:
                os.remove(path)
                total -= size
                print(f'Cache: evicted {os.path.relpath(path, self._root)}')
            except OSError:
                pass


class Prefetcher:
    """Follows the hub's queue and pulls the current song plus the next PREFETCH_COUNT ones."""

    def __init__(self, cache, state_client):
        self._cache = cache
        self._client = state_client
        self._state = None
        self._state_at = 0.0
        self._lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def last_state(self):
        """Most recent state seen from the hub, and how many seconds ago that was."""
        with self._lock:
            return self._state, time.time() - self._state_at if self._state else None

    def remember(self, state):
        """Any fresh state seen by the node, e.g. an answer to the player's sync call."""
        if not isinstance(state, dict) or 'queue' not in state:
            return
        with self._lock:
            self._state, self._state_at = state, time.time()

    def _run(self):
        while True:
            try:
                state, status = self._client.json('GET', '/api/state')
                if status == 200:
                    self.remember(state)
                    self._prefetch(state)
            except HubUnreachable:
                pass
            except Exception as e:
                print(f'Prefetcher: {e}')
            time.sleep(POLL_INTERVAL)

    def _prefetch(self, state):
        items = [item for item in [state.get('current'), *state.get('queue', [])] if item][:1 + config.PREFETCH_COUNT]
        keep = set()
        for item in items:
            for field in ASSET_FIELDS:
                url_path = item.get(field)
                if not url_path:
                    continue
                path = self._cache.ensure(url_path)
                if path:
                    keep.add(path)
            self._ensure_lyrics(item)
        self._cache.evict(keep)

    def _ensure_lyrics(self, item):
        key = item.get('key')
        # Lyrics are only final once the hub reports the song as ready.
        if key and item.get('status') == 'ready' and not self._cache.get_lyrics(key):
            self._cache.fetch_lyrics(key)
