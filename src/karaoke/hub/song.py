"""Audio files of one song on disk: download, vocal separation and key detection.

Everything lives under storage/tracks, named by the song key (ISRC or 'deezer-<id>'):

    <key>.mp3                              the downloaded recording
    <key>.conf                             title and detected key ('scale')
    <key>.lyrics.json                      lyrics, see karaoke.hub.lyrics
    separated/htdemucs/<key>/vocals.mp3    stems written by demucs
    separated/htdemucs/<key>/no_vocals.mp3
"""

import configparser
import subprocess
import sys

import essentia.standard as es
import yt_dlp

from karaoke import config
from karaoke.hub.device import get_device

# Max difference between the song and the chosen YouTube upload duration (seconds)
MAX_SOURCE_DURATION_DIFF = 10
# Essentia names some keys with flats; the UI and the MIDI output use sharps.
_FLAT_TO_SHARP = {'Db': 'C#', 'Eb': 'D#', 'Gb': 'F#', 'Ab': 'G#', 'Bb': 'A#', 'Cb': 'B', 'Fb': 'E'}
_YDL_BASE = {'js_runtimes': {'node': {}}}


class Song:
    def __init__(self, key, title=None, artist=None, album=None, duration=None):
        self.key = key
        self.title = title
        self.artist = artist
        self.album = album
        self.duration = duration

    # --- Files ---

    @property
    def audio_path(self):
        return config.TRACKS_DIR / f'{self.key}.mp3'

    @property
    def config_path(self):
        return config.TRACKS_DIR / f'{self.key}.conf'

    @property
    def vocals_path(self):
        return config.STEMS_DIR / self.key / 'vocals.mp3'

    @property
    def instrumental_path(self):
        return config.STEMS_DIR / self.key / 'no_vocals.mp3'

    def has_audio(self):
        return self.audio_path.is_file() and self.config_path.is_file()

    def has_stems(self):
        return self.vocals_path.is_file() and self.instrumental_path.is_file()

    def is_prepared(self):
        return self.has_audio() and self.has_stems()

    # --- Metadata ---

    def read_config(self):
        if not self.config_path.is_file():
            return None
        parser = configparser.ConfigParser()
        parser.read(self.config_path)
        return parser['track']

    def scale(self):
        conf = self.read_config()
        return conf.get('scale') if conf else None

    def save_config(self):
        """Detects the key of the recording and stores it with the title."""
        print(f'Analyzing {self.key}')
        parser = configparser.ConfigParser()
        parser['track'] = {'isrc': self.key, 'name': self.title or '', 'scale': self.detect_scale()}
        with open(self.config_path, 'w') as f:
            parser.write(f)

    def detect_scale(self):
        """Key of the recording, e.g. 'C# minor'."""
        audio = es.MonoLoader(filename=str(self.audio_path))()
        key, scale, strength = es.KeyExtractor()(audio)
        key = _FLAT_TO_SHARP.get(key, key)
        print(f'Detected key of {self.key}: {key} {scale} (confidence {strength:.2f})')
        return f'{key} {scale}'

    # --- Processing ---

    def find_source(self):
        """URL of the YouTube upload whose duration best matches the song."""
        queries = []
        if not self.key.startswith('deezer-'):
            queries.append(f'ytsearch3:"{self.key}"')
        if self.artist and self.title:
            queries.append(f'ytsearch5:{self.artist} - {self.title}')

        fallback = None
        opts = {**_YDL_BASE, 'quiet': True, 'no_warnings': True, 'extract_flat': 'in_playlist'}
        with yt_dlp.YoutubeDL(opts) as ydl:
            for query in queries:
                entries = [e for e in ydl.extract_info(query, download=False).get('entries') or [] if e.get('url')]
                if not entries:
                    continue
                if not self.duration:
                    return entries[0]['url']
                fallback = fallback or entries[0]['url']
                best = min(entries, key=lambda e: abs((e.get('duration') or 0) - self.duration))
                if abs((best.get('duration') or 0) - self.duration) <= MAX_SOURCE_DURATION_DIFF:
                    return best['url']
        return fallback

    def download(self):
        source = self.find_source()
        if not source:
            print(f'No YouTube source found for {self.key}')
            return

        opts = {
            **_YDL_BASE,
            'format': 'bestaudio/best',
            'outtmpl': str(config.TRACKS_DIR / f'{self.key}.%(ext)s'),
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            print(f'Downloading {self.key} from {source}')
            ydl.download([source])

    def separate(self):
        device = get_device()
        print(f'Separating {self.key} on {device}')
        subprocess.run([sys.executable, '-m', 'demucs', '--mp3', '--two-stems', 'vocals', '--shifts', '1',
                        str(self.audio_path), '--out', str(config.SEPARATED_DIR), '--device', device,
                        '--mp3-preset', '4'])


def rescan_scales():
    """Detects the key of every downloaded song again, e.g. after the detection improved."""
    for audio in sorted(config.TRACKS_DIR.glob('*.mp3')):
        conf = Song(audio.stem).read_config()
        if conf is None:
            continue
        Song(audio.stem, title=conf.get('name')).save_config()
