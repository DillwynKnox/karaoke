"""The karaoke session: song queue, playback state shared by the admin, guest and
player UIs, and a worker that prepares queued songs one after another."""

import json
import os
import threading
import time
import traceback
import uuid

from karaoke import config
from karaoke.hub import deezer, library, lyrics
from karaoke.hub.mix import LEVELS, MAX_DB, MIX_PRESETS, MUTE_DB, make_mix, with_levels
from karaoke.hub.song import Song

PLAYER_TIMEOUT_SECONDS = 3
# Queue, current song and settings survive crashes and restarts.
QUEUE_FILE = config.STORAGE_DIR / 'queue.json'

class KaraokeSession:
    def __init__(self):
        self._lock = threading.Condition()
        self.current = None
        self.queue = []
        # Every song waits for a key press or click on the player before it plays.
        self.started = False
        self.paused = False
        # Incremented whenever the player has to (re)start the current song from 0.
        self.seek_seq = 0
        self.settings = {'lyrics_offset': 0.0, 'key': None}
        # Levels of each mode, adjustable by the admin.
        self.presets = {mode: dict(levels) for mode, levels in MIX_PRESETS.items()}
        self.player = {'last_seen': 0.0}
        self.last_error = None
        self._songs = {}  # song key -> {'step', 'error', 'scale', 'original', 'stems', 'lyrics'}
        self._load_queue()
        threading.Thread(target=self._worker, daemon=True).start()

    # --- Persistence ---

    def _load_queue(self):
        try:
            with open(QUEUE_FILE) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        self.current = data.get('current')
        self.queue = data.get('queue', [])
        self.settings.update(data.get('settings', {}))
        for mode, levels in data.get('presets', {}).items():
            if mode in self.presets:
                self.presets[mode] = with_levels(self.presets[mode], levels)
        for item in [self.current, *self.queue]:
            if item and item['key'] not in self._songs:
                self._songs[item['key']] = self._scan(item['key'])

    def _save_queue(self):
        """Writes queue and settings atomically. Call with the lock held."""
        tmp = QUEUE_FILE.with_suffix('.json.tmp')
        with open(tmp, 'w') as f:
            json.dump({'current': self.current, 'queue': self.queue, 'settings': self.settings,
                       'presets': self.presets}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, QUEUE_FILE)

    # --- Queue and playback ---

    def add(self, deezer_id, mix_values=None):
        mix = make_mix(mix_values or {}, self.presets)
        info = deezer.track(deezer_id)
        key = library.song_key(info)
        library.put(key, info)
        item = {'id': uuid.uuid4().hex[:8], 'key': key, 'mix': mix, **info}

        with self._lock:
            song = self._songs.get(key)
            if song is None or song['error']:
                self._songs[key] = self._scan(key)
            if self.current is None:
                self._start(item)
            else:
                self.queue.append(item)
            self._save_queue()
            self._lock.notify_all()
        return item

    @staticmethod
    def _scan(key):
        """What already exists on disk for a song, e.g. from an earlier session."""
        song = Song(key)
        return {
            'step': None,
            'error': None,
            'original': song.has_audio(),
            'stems': song.has_stems(),
            'lyrics': lyrics.is_settled(key),
            'scale': song.scale(),
        }

    def _start(self, item):
        self.current = item
        self.started = False
        self.paused = False
        self.seek_seq += 1
        self.settings['key'] = None

    def _advance(self):
        if self.queue:
            self._start(self.queue.pop(0))
        else:
            self.current = None
        self._save_queue()
        self._lock.notify_all()

    def command(self, action):
        with self._lock:
            if action == 'toggle':
                # Before the song was started, play starts it (like Space on the player).
                self.paused = self.started and not self.paused
                self.started = True
            elif action == 'play':
                self.started = True
                self.paused = False
            elif action == 'pause':
                self.paused = True
            elif action == 'restart':
                self.seek_seq += 1
                self.paused = False
            elif action == 'skip':
                self._advance()
            else:
                raise ValueError(f'Unknown action {action}')

    def remove(self, item_id):
        with self._lock:
            if self.current and self.current['id'] == item_id:
                self._advance()
            self.queue = [item for item in self.queue if item['id'] != item_id]
            self._save_queue()

    def play_next(self, item_id):
        with self._lock:
            for i, item in enumerate(self.queue):
                if item['id'] == item_id:
                    self.queue.insert(0, self.queue.pop(i))
                    self._save_queue()
                    self._lock.notify_all()
                    return

    def update_settings(self, values):
        with self._lock:
            if 'lyrics_offset' in values:
                self.settings['lyrics_offset'] = round(min(10.0, max(-10.0, float(values['lyrics_offset']))), 1)
            if 'key' in values:
                self.settings['key'] = str(values['key']) if values['key'] else None
            self._save_queue()

    def update_mix(self, values):
        """Changes the mix of the song that is playing right now."""
        with self._lock:
            if not self.current:
                raise ValueError('Nothing is playing')
            if values.get('mode'):
                self.current['mix'] = make_mix({'mode': values['mode']}, self.presets)
            else:
                levels = {level: values[level] for level in LEVELS if values.get(level) is not None}
                self.current['mix'] = make_mix({**self.current['mix'], **levels}, self.presets)
            self._save_queue()

    def update_preset(self, mode, values):
        """Changes the levels of a mode, also for the current and queued songs in that mode.
        Songs with a custom mix keep their levels."""
        with self._lock:
            if mode not in self.presets:
                raise ValueError(f'Unknown mode {mode}')
            self.presets[mode] = with_levels(self.presets[mode], values)
            for item in [self.current, *self.queue]:
                if item and item['mix']['mode'] == mode:
                    item['mix'] = {'mode': mode, **self.presets[mode]}
            self._save_queue()

    def set_lyrics(self, key, text):
        """Replaces a song's lyrics with pasted text; the song is ready again once aligned."""
        lyrics.set_lyrics(key, text)
        with self._lock:
            if key in self._songs:
                self._songs[key]['lyrics'] = False
            self._lock.notify_all()

    # --- Player ---

    def player_start(self, item_id):
        with self._lock:
            if self.current and self.current['id'] == item_id and self._is_ready(self.current):
                self.started = True
                self.paused = False

    def player_sync(self, report):
        player = {
            'last_seen': time.time(),
            'item_id': report.get('item_id'),
            'position': float(report.get('position') or 0),
            'duration': float(report.get('duration') or 0),
            'playing': bool(report.get('playing')),
            'unlocked': bool(report.get('unlocked')),
        }
        with self._lock:
            self.player = player

    def player_ended(self, item_id):
        with self._lock:
            if self.current and self.current['id'] == item_id:
                self._advance()

    def state(self):
        with self._lock:
            player = dict(self.player)
            player['connected'] = time.time() - player['last_seen'] < PLAYER_TIMEOUT_SECONDS
            return {
                'current': self._item_state(self.current) if self.current else None,
                'queue': [self._item_state(item) for item in self.queue],
                'started': self.started,
                'paused': self.paused,
                'seek_seq': self.seek_seq,
                'settings': dict(self.settings),
                'player': player,
                'last_error': self.last_error,
                'mix': {'presets': {mode: dict(levels) for mode, levels in self.presets.items()},
                        'defaults': MIX_PRESETS, 'mute_db': MUTE_DB, 'max_db': MAX_DB},
            }

    def _is_ready(self, item):
        song = self._songs[item['key']]
        # Songs only start with separated tracks and finished lyrics alignment, also in
        # Boring mode: aligning against the vocal track is far more reliable than the mix.
        return song['original'] and song['stems'] and song['lyrics']

    def _item_state(self, item):
        song = self._songs[item['key']]
        if self._is_ready(item):
            status = 'ready'
        else:
            status = 'error' if song['error'] else song['step'] or 'queued'

        state = {**item, 'status': status, 'error': song['error'], 'scale': song['scale']}
        if song['original']:
            state['original'] = f"/storage/tracks/{item['key']}.mp3"
        if song['stems']:
            base = f"/storage/tracks/separated/htdemucs/{item['key']}"
            state.update(vocals=f'{base}/vocals.mp3', no_vocals=f'{base}/no_vocals.mp3')
        return state

    # --- Worker ---

    def _next_job(self):
        for item in [self.current, *self.queue]:
            if item and not self._is_ready(item) and not self._songs[item['key']]['error']:
                return item
        return None

    def _worker(self):
        while True:
            with self._lock:
                while (item := self._next_job()) is None:
                    self._lock.wait()
                song = dict(self._songs[item['key']])
            try:
                self._prepare(item, song)
            except Exception as e:
                traceback.print_exc()
                self._fail(item, str(e))

    def _fail(self, item, error):
        with self._lock:
            key = item['key']
            self._songs[key].update(step=None, error=error)
            self.last_error = {'id': uuid.uuid4().hex[:8], 'title': item['title'], 'error': error}
            self.queue = [i for i in self.queue if i['key'] != key]
            if self.current and self.current['key'] == key:
                self._advance()
            self._save_queue()

    def _set_song(self, key, **values):
        with self._lock:
            self._songs[key].update(values)

    def _prepare(self, item, state):
        key = item['key']
        song = Song(key, title=item['title_short'], artist=item['artist'],
                    album=item['album'], duration=item['duration'])

        if not state['original']:
            if not song.audio_path.is_file():
                self._set_song(key, step='downloading')
                song.download()
                if not song.audio_path.is_file():
                    raise RuntimeError('No matching YouTube audio found')

            if not song.config_path.is_file():
                self._set_song(key, step='analyzing')
                song.save_config()
            self._set_song(key, step=None, original=True, scale=song.scale())

        if not state['stems']:
            self._set_song(key, step='separating')
            song.separate()
            if not song.has_stems():
                raise RuntimeError('Vocal separation failed')
            self._set_song(key, step=None, stems=True)

        if not state['lyrics']:
            self._set_song(key, step='lyrics')
            # A second attempt covers lyrics replaced while an older alignment was running.
            for _ in range(2):
                lyrics.ensure_lyrics(key, item['title_short'], item['artist'], item['album'], item['duration'],
                                     song.vocals_path, background=False)
                while lyrics.is_aligning(key):
                    time.sleep(0.5)
                if lyrics.is_settled(key):
                    break
            self._set_song(key, step=None, lyrics=True)
