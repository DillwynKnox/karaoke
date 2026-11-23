"""Synced karaoke lyrics.

Lyrics come from LRCLIB (line-level LRC timestamps). Once the vocal stem exists,
the lyrics are force-aligned against it with a multilingual wav2vec2 CTC model to
get word- and syllable-level timestamps. Alignment runs over the whole song, so a different
intro length between the downloaded audio and the LRC source is corrected too.
"""

import functools
import json
import os
import re
import threading
import time
import unicodedata

import numpy as np
import requests

from karaoke import config
from karaoke.hub import librettos
from karaoke.hub.device import get_device

LRCLIB_URL = 'https://lrclib.net/api'
ALIGN_MODEL = config.LYRICS_ALIGN_MODEL
ALIGN_ENABLED = config.LYRICS_ALIGN
SAMPLE_RATE = 16000
FRAME_SECONDS = 320 / SAMPLE_RATE  # wav2vec2 conv stack downsamples by 320
CHUNK_SECONDS = 30
CHUNK_HOP_SECONDS = 15
NOT_FOUND_RETRY_SECONDS = 24 * 3600
# Bump when the lookup finds more songs, so earlier 'not_found' results are retried.
LOOKUP_VERSION = 3
# Max distance of the search result's duration from the track's duration.
MAX_DURATION_DIFF = 15
# A synced LRC counts as made for a recording only this close to its duration, see lyrics_match.
MAX_MATCH_DURATION_DIFF = 3
# An aligned line falls back to its offset-corrected LRC timing only if it is both
# further than MAX_LINE_DRIFT from it and has a mean word probability below
# MIN_LINE_SCORE. Confident alignments win because LRCLIB timings can be wrong.
MAX_LINE_DRIFT = 4.0
# Log-probability cost of jumping back to an earlier line when aligning unsynced lyrics;
# None disables repeats.
REPEAT_PENALTY = -8.0
MIN_LINE_SCORE = 0.15
# If more than this share of confidently aligned lines drifts from the LRC timing, the LRC
# belongs to another version of the song (e.g. an extended mix) and is not used as fallback.
CONFIDENT_LINE_SCORE = 0.5
MAX_CONFIDENT_DRIFT_SHARE = 0.1
# Bump when the alignment result improves, so already aligned songs are aligned again.
ALIGN_VERSION = 2
# A syllable starts at most this long before its vowel, see _syllable_timings.
MAX_SYLLABLE_ONSET = 0.15
# Frequent words that tell the language of the lyrics for hyphenation; the first one wins ties.
_LANGUAGE_WORDS = {
    'de_DE': {'der', 'die', 'das', 'und', 'ist', 'nicht', 'ich', 'du', 'wir', 'ein', 'eine', 'mein', 'dein',
              'mich', 'dich', 'mir', 'dir', 'auf', 'mit', 'zu', 'von', 'dem', 'den', 'es', 'wie', 'aber', 'auch'},
    'en_US': {'the', 'and', 'is', 'not', 'you', 'we', 'they', 'my', 'your', 'me', 'it', 'to', 'of', 'on',
              'with', 'be', 'that', 'this', 'what', 'when', 'but', 'just', 'all', 'can', 'love', 'don'},
}

_TITLE_SUFFIX = re.compile(r'\s+-\s+[^-]*\b(remaster(ed)?|version|edit|mix|live|mono|stereo|single|radio)\b.*$', re.I)
# Title words of recordings that differ from the studio version although the length may match.
_VERSION_TAG = re.compile(r'\b(live|(?:re)?mix|acoustic|unplugged|instrumental|karaoke|cover\w*)\b', re.I)
_TITLE_FEAT = re.compile(r'\s*[(\[](feat\.?|ft\.?|with)\s[^)\]]*[)\]]', re.I)
_MOVEMENT_NUMBER = re.compile(r'^\s*(No\.|Nr\.)\s*\d+[a-z]?\s*[,.]?\s*', re.I)
_MOVEMENT_KIND = re.compile(
    r'^\s*(Chorus|Chor|Choral|Recit\.?|Rezitativ|Arioso|Arie|Aria|Duett|Terzett|Quartett|Sinfonia)\b\s*[.:]?', re.I)
_LRC_TIME = re.compile(r'\[(\d+):(\d+)(?:[.:](\d+))?\]')
_LRC_WORD_TAG = re.compile(r'<\d+:\d+(?:[.:]\d+)?>')
_LETTERS = re.compile(r'[^\W\d_]+')

_jobs = set()
_jobs_lock = threading.Lock()
_align_lock = threading.Lock()
_model = None
_uroman = None


def lyrics_path(key):
    return config.TRACKS_DIR / f'{key}.lyrics.json'


def load_lyrics(key):
    try:
        with open(lyrics_path(key)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_lyrics(key, data):
    tmp = lyrics_path(key).with_suffix('.json.tmp')
    with open(tmp, 'w') as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, lyrics_path(key))


def is_aligning(key):
    with _jobs_lock:
        return key in _jobs


def _lookup_outdated(data):
    """A 'not_found' result is looked up again after a day or when the lookup improved."""
    return data['status'] == 'not_found' and (
        data.get('lookup_version', 1) < LOOKUP_VERSION or
        time.time() - data.get('fetched_at', 0) > NOT_FOUND_RETRY_SECONDS)


def _needs_alignment(data):
    """True for lyrics that are not aligned yet or were aligned by an older ALIGN_VERSION."""
    return data['status'] == 'ok' and bool(data['lines']) and not data.get('align_error') \
        and data.get('align_version', 0) < ALIGN_VERSION


def is_settled(key):
    """True when the cached lyrics need no more work: aligned, alignment failed or
    disabled, or there are no lyrics for the song."""
    data = load_lyrics(key)
    if data is None or _lookup_outdated(data):
        return False
    return not (ALIGN_ENABLED and _needs_alignment(data))


# --- LRCLIB ---

def clean_title(title):
    """Strip streaming-catalogue suffixes like " - Remastered 2011" or "(feat. X)"."""
    cleaned = _TITLE_FEAT.sub('', _TITLE_SUFFIX.sub('', title)).strip()
    return cleaned or title


def _lrclib(endpoint, params):
    resp = requests.get(f'{LRCLIB_URL}/{endpoint}', params=params,
                        headers={'User-Agent': 'karaoke'}, timeout=10)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def fetch_lrclib(title, artist, album=None, duration=None):
    title = clean_title(title)
    exact = None
    if duration:
        params = {'track_name': title, 'artist_name': artist, 'duration': round(duration)}
        if album:
            params['album_name'] = album
        exact = _lrclib('get', params)
        if exact and exact.get('syncedLyrics'):
            return exact

    results = _lrclib('search', {'track_name': title, 'artist_name': artist}) or []
    results = [r for r in results if r.get('syncedLyrics') or r.get('plainLyrics')]
    if duration:
        results = [r for r in results if abs((r.get('duration') or 0) - duration) <= MAX_DURATION_DIFF]
    if not results:
        if exact and exact.get('plainLyrics'):
            return exact
        return _find_by_movement(title)

    return min(results, key=lambda r: (
        not r.get('syncedLyrics'),
        abs((r.get('duration') or 0) - duration) if duration else 0,
    ))


def _version_tags(text):
    return {tag.lower().removeprefix('re') for tag in _VERSION_TAG.findall(text)}


@functools.lru_cache(maxsize=512)
def search_lyrics(title, artist):
    """(duration, track and album name, synced) of the LRCLIB results with lyrics."""
    results = _lrclib('search', {'track_name': title, 'artist_name': artist}) or []
    return tuple((r.get('duration') or 0, f"{r.get('trackName') or ''} {r.get('albumName') or ''}",
                  bool(r.get('syncedLyrics')))
                 for r in results if r.get('syncedLyrics') or r.get('plainLyrics'))


def lyrics_match(title, duration, entries):
    """How well the lyrics on LRCLIB fit a recording, shown before a song is added:
    'synced' when there is a synced LRC of this version, 'other_version' when the synced lyrics
    belong to another version (LRCLIB often has only the studio version, also under the names of
    live recordings and dance mixes), 'plain' for unsynced lyrics, 'none' without lyrics.
    Without a fitting LRC the timing comes from the alignment alone, which fails more often.
    `entries` is the search_lyrics result for the song."""
    if librettos.find(title):
        return 'plain'
    synced = [(entry_duration, name) for entry_duration, name, is_synced in entries if is_synced]
    tags = _version_tags(title)
    if any(abs(entry_duration - duration) <= MAX_MATCH_DURATION_DIFF and tags <= _version_tags(name)
           for entry_duration, name in synced):
        return 'synced'
    if synced:
        return 'other_version'
    return 'plain' if entries else 'none'


def movement_title(title):
    """'Weihnachtsoratorium, BWV 248, Pt. 1: No. 8, Großer Herr (Arie [Bass])' -> 'Großer Herr'."""
    text = re.sub(r'nn(?=\()', '', title)  # Deezer sometimes renders line breaks as 'nn'
    text = re.sub(r'\([^)]*\)|\[[^\]]*\]', ' ', text).split(':')[-1]
    text = _MOVEMENT_KIND.sub('', _MOVEMENT_NUMBER.sub('', text))
    return re.sub(r'\s+', ' ', text).strip(' ."\'“”„,')


def _normalize(text):
    return re.sub(r'[\W_]+', ' ', text.lower()).strip()


def _find_by_movement(title):
    """Classical recordings are listed under the conductor with a long work title, while
    LRCLIB has the text under the composer. Search by the movement instead and use the text
    unsynced, because the timing of another recording does not fit."""
    if ':' not in title:
        return None
    movement = movement_title(title)
    if len(movement) < 4:
        return None

    wanted = _normalize(movement)
    for result in _lrclib('search', {'q': movement}) or []:
        text = result.get('plainLyrics') or '\n'.join(
            content for _, content in parse_lrc(result.get('syncedLyrics') or '') if content)
        if wanted in _normalize(result.get('trackName') or '') and text.strip():
            return {**result, 'syncedLyrics': None, 'plainLyrics': text}
    return None


def parse_lrc(text):
    """Returns sorted (start_seconds, text) entries; empty text marks a break."""
    entries = []
    for raw in text.splitlines():
        raw = raw.strip()
        stamps = []
        pos = 0
        while (m := _LRC_TIME.match(raw, pos)):
            frac = m.group(3) or '0'
            stamps.append(int(m.group(1)) * 60 + int(m.group(2)) + int(frac) / 10 ** len(frac))
            pos = m.end()
        content = _LRC_WORD_TAG.sub('', raw[pos:]).strip()
        entries.extend((stamp, content) for stamp in stamps)
    entries.sort(key=lambda e: e[0])
    # Some LRCLIB uploads add a translation under the same timestamp; keep the first line.
    return [e for i, e in enumerate(entries) if i == 0 or e[0] != entries[i - 1][0]]


def _spread_words(words, start, end):
    """Estimate word timings by distributing the line duration by word length."""
    weights = np.array([len(w) + 1 for w in words], dtype=float)
    bounds = start + (end - start) * np.concatenate(([0], np.cumsum(weights) / weights.sum()))
    return [{'word': w, 'start': round(bounds[i], 3), 'end': round(bounds[i + 1], 3), 'estimated': True}
            for i, w in enumerate(words)]


def _line_segments(entries, duration=None):
    segments = []
    for i, (start, text) in enumerate(entries):
        if not text:
            continue
        words = text.split()
        next_start = entries[i + 1][0] if i + 1 < len(entries) else (duration or start + 6)
        end = max(start + 0.3, min(next_start, start + 1.5 + 0.6 * len(words)))
        segments.append({'text': text, 'start': start, 'end': round(end, 3),
                         'words': _spread_words(words, start, end)})
    return segments


def _payload(result, duration):
    if result.get('instrumental'):
        return {'status': 'instrumental', 'fetched_at': time.time()}

    segments = _line_segments(parse_lrc(result.get('syncedLyrics') or ''), duration)
    lines = [s['text'] for s in segments] or \
        [line.strip() for line in (result.get('plainLyrics') or '').splitlines() if line.strip()]
    return {
        'status': 'ok',
        'source': 'lrclib',
        'lookup_version': LOOKUP_VERSION,
        'lrclib_id': result.get('id'),
        'synced': bool(segments),
        'aligned': False,
        'lines': lines,
        'segments': segments,
        'fetched_at': time.time(),
    }


# --- Forced alignment ---

def _load_model(device):
    global _model
    if _model is None or _model[0] != device:
        import torch
        from huggingface_hub import hf_hub_download
        from transformers import AutoModelForCTC

        dtype = torch.float16 if device == 'cuda' else torch.float32
        model = AutoModelForCTC.from_pretrained(ALIGN_MODEL, dtype=dtype).to(device).eval()
        with open(hf_hub_download(ALIGN_MODEL, 'vocab.json')) as f:
            vocab = json.load(f)
        _model = (device, model, vocab, dtype)
    return _model


def _romanize(word):
    global _uroman
    if _uroman is None:
        import uroman
        _uroman = uroman.Uroman()
    text = unicodedata.normalize('NFKD', _uroman.romanize_string(word).lower())
    return ''.join(c for c in text if not unicodedata.combining(c))


def _emissions(vocals_path, device):
    """Log-probabilities per frame. The audio is processed in overlapping windows and every
    frame is taken from the window where it is most central, so words at window edges keep
    their context (measured: clearly fewer lines off by more than 2 s)."""
    import librosa
    import torch

    _, model, vocab, dtype = _load_model(device)
    audio, _ = librosa.load(vocals_path, sr=SAMPLE_RATE, mono=True)
    window = CHUNK_SECONDS * SAMPLE_RATE
    hop = CHUNK_HOP_SECONDS * SAMPLE_RATE
    min_len = SAMPLE_RATE // 10
    frame = int(FRAME_SECONDS * SAMPLE_RATE)
    total = len(audio) // frame + 1

    emissions = None
    distance = np.full(total, np.inf)
    with torch.inference_mode():
        for offset in range(0, max(1, len(audio) - window + hop), hop):
            x = audio[offset:offset + window]
            if len(x) < min_len:
                x = np.pad(x, (0, min_len - len(x)))
            x = (x - x.mean()) / np.sqrt(x.var() + 1e-7)
            logits = model(torch.from_numpy(x).to(device, dtype)[None]).logits[0]
            chunk = torch.log_softmax(logits.float(), dim=-1).cpu().numpy()
            if emissions is None:
                emissions = np.zeros((total, chunk.shape[1]), dtype=np.float32)

            first = offset // frame
            n = min(len(chunk), total - first)
            chunk_distance = np.abs(np.arange(n) - len(chunk) / 2)
            closer = chunk_distance < distance[first:first + n]
            emissions[first:first + n][closer] = chunk[:n][closer]
            distance[first:first + n][closer] = chunk_distance[closer]

    covered = np.nonzero(np.isfinite(distance))[0][-1] + 1
    return emissions[:covered], vocab


def _ctc_viterbi(emissions, tokens, blank):
    """Most likely monotonic CTC path through `tokens`. Returns the state per frame
    (even states are blanks, odd state 2k+1 is token k) and the labels, or None if impossible."""
    num_frames = len(emissions)
    num_states = 2 * len(tokens) + 1
    if num_frames < num_states // 2:
        return None

    labels = np.full(num_states, blank)
    labels[1::2] = tokens
    # A token may be reached from two states back unless it repeats the previous token.
    can_skip = np.zeros(num_states, dtype=bool)
    can_skip[3::2] = labels[3::2] != labels[1:-2:2]

    alpha = np.full(num_states, -np.inf, dtype=np.float32)
    alpha[0] = emissions[0, blank]
    alpha[1] = emissions[0, labels[1]]
    back = np.zeros((num_frames, num_states), dtype=np.int8)
    prev1 = np.empty(num_states, dtype=np.float32)
    prev2 = np.empty(num_states, dtype=np.float32)

    for t in range(1, num_frames):
        prev1[0] = -np.inf
        prev1[1:] = alpha[:-1]
        prev2[:2] = -np.inf
        prev2[2:] = alpha[:-2]
        prev2[~can_skip] = -np.inf

        step = (prev1 > alpha).astype(np.int8)
        best = np.maximum(prev1, alpha)
        use_skip = prev2 > best
        step[use_skip] = 2
        best[use_skip] = prev2[use_skip]

        back[t] = step
        alpha = best + emissions[t, labels]

    state = num_states - 1 if alpha[-1] >= alpha[-2] else num_states - 2
    if not np.isfinite(alpha[state]):
        return None

    path = np.empty(num_frames, dtype=np.int64)
    for t in range(num_frames - 1, -1, -1):
        path[t] = state
        state -= back[t, state]
    return path, labels


def _ctc_viterbi_repeats(emissions, tokens, blank, line_ranges, repeat_penalty, required_lines):
    """Like _ctc_viterbi, but the path may jump from the end of a line back to the start of
    the same or an earlier line, for lyrics that list repeated lines only once.

    Every line up to `required_lines` has to be sung at least once; after that the path may
    end after any line (e.g. an aria returning to its first part). This uses two copies of
    the states: before (0) and after (1) all required lines were reached."""
    num_frames = len(emissions)
    size = 2 * len(tokens) + 1
    if num_frames < size // 2:
        return None

    labels = np.full(size, blank)
    labels[1::2] = tokens
    can_skip = np.zeros(size, dtype=bool)
    can_skip[3::2] = labels[3::2] != labels[1:-2:2]
    labels2 = np.concatenate([labels, labels])
    can_skip2 = np.concatenate([can_skip, can_skip])

    num_lines = len(line_ranges)
    order = np.arange(num_lines)
    line_start = np.array([2 * first for first, _ in line_ranges])  # blank before the line
    line_last = np.array([2 * end - 1 for _, end in line_ranges])   # last token of the line
    complete = order[min(required_lines, num_lines) - 1:]           # lines ending the required part
    switch = np.concatenate([line_last[complete], line_last[complete] + 1])

    alpha = np.full(2 * size, -np.inf, dtype=np.float32)
    alpha[0] = emissions[0, blank]
    alpha[1] = emissions[0, labels[1]]
    # 0 stay, 1/2 advance, 3 jump back, 4 required part complete
    back = np.zeros((num_frames, 2 * size), dtype=np.int8)
    jump_line = np.zeros((num_frames, 2, num_lines), dtype=np.int32)
    jump_from_blank = np.zeros((num_frames, 2, num_lines), dtype=bool)
    prev1 = np.empty(2 * size, dtype=np.float32)
    prev2 = np.empty(2 * size, dtype=np.float32)

    for t in range(1, num_frames):
        prev1[0] = -np.inf
        prev1[1:] = alpha[:-1]
        prev1[size] = -np.inf
        prev2[:2] = -np.inf
        prev2[2:] = alpha[:-2]
        prev2[size:size + 2] = -np.inf
        prev2[~can_skip2] = -np.inf

        step = (prev1 > alpha).astype(np.int8)
        best = np.maximum(prev1, alpha)
        use_skip = prev2 > best
        step[use_skip] = 2
        best[use_skip] = prev2[use_skip]

        candidate = alpha[switch]
        better = candidate > best[switch + size]
        best[switch[better] + size] = candidate[better]
        step[switch[better] + size] = 4

        for copy in (0, 1):
            offset = copy * size
            end_token = alpha[offset + line_last]
            end_blank = alpha[offset + line_last + 1]
            from_blank = end_blank > end_token
            line_end = np.where(from_blank, end_blank, end_token)
            # Best end of this or any later line, i.e. every line a jump to this line can come from.
            reversed_end = line_end[::-1]
            is_max = reversed_end >= np.maximum.accumulate(reversed_end)
            source = (num_lines - 1 - np.maximum.accumulate(np.where(is_max, order, 0)))[::-1]
            jump = line_end[source] + repeat_penalty
            for target in (offset + line_start, offset + line_start + 1):
                better = jump > best[target]
                best[target[better]] = jump[better]
                step[target[better]] = 3
            jump_line[t, copy] = source
            jump_from_blank[t, copy] = from_blank[source]

        back[t] = step
        alpha = best + emissions[t, labels2]

    ends = np.concatenate([switch, size + line_last, size + line_last + 1])
    state = int(ends[np.argmax(alpha[ends])])
    if not np.isfinite(alpha[state]):
        return None

    start_line = np.full(size, -1)
    start_line[line_start] = order
    start_line[line_start + 1] = order
    path = np.empty(num_frames, dtype=np.int64)
    for t in range(num_frames - 1, -1, -1):
        copy, local = divmod(state, size)
        path[t] = local
        code = back[t, state]
        if code == 3:
            line = start_line[local]
            state = copy * size + int(line_last[jump_line[t, copy, line]] + jump_from_blank[t, copy, line])
        elif code == 4:
            state -= size
        else:
            state -= int(code)
    return path, labels


def _interpolate_words(words):
    """Give words the aligner could not place a slot between their neighbours."""
    timed = [w for w in words if w['start'] is not None]
    if not timed:
        return
    for i, w in enumerate(words):
        if w['start'] is not None:
            continue
        prev_end = next((words[j]['end'] for j in range(i - 1, -1, -1) if words[j]['start'] is not None),
                        timed[0]['start'])
        next_start = next((words[j]['start'] for j in range(i + 1, len(words)) if words[j]['start'] is not None),
                          timed[-1]['end'])
        w['start'] = prev_end
        w['end'] = max(prev_end, (prev_end + next_start) / 2)


def _hyphenator(lines):
    """Pyphen dictionary for the language of the lyrics, guessed from frequent words."""
    import pyphen
    words = _LETTERS.findall(' '.join(lines).lower())
    hits = {lang: sum(w in common for w in words) for lang, common in _LANGUAGE_WORDS.items()}
    return pyphen.Pyphen(lang=max(hits, key=hits.get))


def _syllables(word, hyphenator, vocab):
    """(text, token ids) per syllable of a word; the texts add up to the word. Only letter runs
    are hyphenated, and pieces without an alignable character (e.g. punctuation) are merged
    into their neighbour."""
    cuts = sorted({0, len(word)} | {m.start() + pos for m in _LETTERS.finditer(word)
                                    for pos in hyphenator.positions(m.group())})
    pieces = []
    for a, b in zip(cuts, cuts[1:]):
        text = word[a:b]
        ids = [vocab[c] for c in _romanize(text) if c in vocab and vocab[c] > 3]
        if pieces and not (ids and pieces[-1][1]):
            pieces[-1] = (pieces[-1][0] + text, pieces[-1][1] + ids)
        else:
            pieces.append((text, ids))
    return pieces


def _syllable_timings(syllables, token_starts, start, end):
    """Syllable timings within an aligned word. The CTC model marks every character with a
    single frame and a held note shows up as blank frames after it, so a syllable lasts until
    the next one starts. A syllable starts at most MAX_SYLLABLE_ONSET before its vowel, because
    its consonants are often placed early, inside the held vowel of the syllable before."""
    onset_frames = round(MAX_SYLLABLE_ONSET / FRAME_SECONDS)
    timings = []
    ti = 0
    for text, num_tokens, vowel in syllables:
        frame = token_starts[ti]
        if vowel is not None:
            frame = max(frame, token_starts[ti + vowel] - onset_frames)
        timings.append({'text': text, 'start': round(frame * FRAME_SECONDS, 3)})
        ti += num_tokens
    timings[0]['start'] = start
    for syllable, following in zip(timings, timings[1:]):
        syllable['end'] = following['start']
    timings[-1]['end'] = end
    return timings


def _pass_segments(data, words, emissions, labels, path, frames):
    """Line segments for one forward run of the alignment path."""
    token_frames = frames[path[frames] % 2 == 1]
    num_tokens = len(labels) // 2
    token_idx = (path[token_frames] - 1) // 2
    starts = np.full(num_tokens, len(path))
    ends = np.zeros(num_tokens, dtype=np.int64)
    prob_sum = np.zeros(num_tokens)
    counts = np.zeros(num_tokens)
    np.minimum.at(starts, token_idx, token_frames)
    np.maximum.at(ends, token_idx, token_frames + 1)
    np.add.at(prob_sum, token_idx, np.exp(emissions[token_frames, labels[path[token_frames]]]))
    np.add.at(counts, token_idx, 1)

    lines = {}
    ti = 0
    for li, word, ids, syllables in words:
        span = slice(ti, ti + len(ids))
        ti += len(ids)
        if ids and counts[span].all():
            timing = {
                'word': word,
                'start': round(starts[span.start] * FRAME_SECONDS, 3),
                'end': round(ends[span.stop - 1] * FRAME_SECONDS, 3),
                'score': round(float(prob_sum[span].sum() / counts[span].sum()), 3),
            }
            if len(syllables) > 1:
                timing['syllables'] = _syllable_timings(syllables, starts[span], timing['start'], timing['end'])
            lines.setdefault(li, []).append(timing)
        else:
            lines.setdefault(li, []).append({'word': word, 'start': None, 'end': None, 'estimated': True})

    segments = []
    for li, line_words in lines.items():
        _interpolate_words(line_words)
        if line_words[0]['start'] is None:
            continue  # line not sung in this run
        scores = [w['score'] for w in line_words if 'score' in w]
        segments.append({'text': data['lines'][li], 'start': line_words[0]['start'],
                         'end': line_words[-1]['end'], 'words': line_words, 'line': li,
                         'score': round(float(np.mean(scores)), 3) if scores else 0.0})
    return segments


def align_lyrics(data, vocals_path, device):
    emissions, vocab = _emissions(vocals_path, device)
    blank = vocab['<blank>']

    hyphenator = _hyphenator(data['lines'])
    vowel_ids = {vocab[c] for c in 'aeiouy' if c in vocab}

    words = []  # (line index, word, token ids, syllables as (text, token count, vowel token or None))
    line_ranges = {}  # line index -> (first token, end token)
    num_tokens = 0
    for li, line in enumerate(data['lines']):
        for word in line.split():
            pieces = _syllables(word, hyphenator, vocab)
            ids = [t for _, piece_ids in pieces for t in piece_ids]
            syllables = [(text, len(piece_ids), next((i for i, t in enumerate(piece_ids) if t in vowel_ids), None))
                         for text, piece_ids in pieces]
            words.append((li, word, ids, syllables))
            if ids:
                line_ranges[li] = (line_ranges.get(li, (num_tokens,))[0], num_tokens + len(ids))
                num_tokens += len(ids)

    tokens = np.array([t for _, _, ids, _ in words for t in ids], dtype=np.int64)
    if not len(tokens):
        raise ValueError('lyrics contain no alignable characters')
    # Synced LRC already lists every repetition; unsynced texts (librettos, pasted lyrics) do not.
    if REPEAT_PENALTY is not None and not data.get('synced'):
        required = data.get('required_lines') or len(data['lines'])
        required_ranges = max(1, sum(li < required for li in line_ranges))
        result = _ctc_viterbi_repeats(emissions, tokens, blank, list(line_ranges.values()),
                                      REPEAT_PENALTY, required_ranges)
    else:
        result = _ctc_viterbi(emissions, tokens, blank)
    if result is None:
        raise ValueError('lyrics do not fit the vocal track')
    path, labels = result

    # A backward jump of the path starts another run through the lyrics.
    runs = np.split(np.arange(len(path)), np.nonzero(np.diff(path) < 0)[0] + 1)
    segments = [seg for run in runs for seg in _pass_segments(data, words, emissions, labels, path, run)]

    # Guard against local misalignment (e.g. LRCLIB text missing a repeated chorus)
    # by comparing with the LRC timing, corrected for a global offset.
    offset = 0.0
    fallbacks = 0
    lrc = data.get('segments') if data.get('synced') else None
    if lrc and len(lrc) == len(data['lines']) and segments:
        offset = float(np.median([s['start'] - lrc[s['line']]['start'] for s in segments]))
        drifting = [abs(s['start'] - lrc[s['line']]['start'] - offset) > MAX_LINE_DRIFT for s in segments]
        confident = [s['score'] >= CONFIDENT_LINE_SCORE for s in segments]
        confident_drifting = sum(d and c for d, c in zip(drifting, confident))
        lrc_fits = confident_drifting <= MAX_CONFIDENT_DRIFT_SHARE * max(1, sum(confident))
        if not lrc_fits:
            print(f'LRC timing does not fit this recording ({confident_drifting} confident lines drift), no fallback')
        for i, seg in enumerate(segments):
            ref = lrc[seg['line']]
            if lrc_fits and drifting[i] and seg['score'] < MIN_LINE_SCORE:
                fallbacks += 1
                segments[i] = {
                    'text': ref['text'], 'line': seg['line'], 'score': seg['score'],
                    'start': round(ref['start'] + offset, 3), 'end': round(ref['end'] + offset, 3),
                    'words': [{**w, 'start': round(w['start'] + offset, 3), 'end': round(w['end'] + offset, 3)}
                              for w in ref['words']],
                }

    for prev, seg in zip(segments, segments[1:]):
        if seg['start'] < prev['end']:
            prev['end'] = max(prev['start'], seg['start'])

    print(f'Lyrics alignment: {len(segments)} lines, offset {offset:+.2f}s, {fallbacks} lines kept LRC timing')
    # The LRC timing is kept for aligning again after an ALIGN_VERSION bump.
    return {**data, 'aligned': True, 'align_version': ALIGN_VERSION, 'offset': round(offset, 3),
            'segments': segments, **({'lrc_segments': lrc} if data.get('synced') else {})}


def _unsynced_payload(lines, source, **extra):
    lines = [line.strip() for line in lines if line.strip()]
    return {
        'status': 'ok' if lines else 'not_found',
        'source': source,
        'lookup_version': LOOKUP_VERSION,
        'synced': False,
        'aligned': False,
        'lines': lines,
        'segments': [],
        'fetched_at': time.time(),
        **extra,
    }


def set_lyrics(key, text):
    """Replaces the lyrics of a song with pasted text; it is aligned again afterwards."""
    data = _unsynced_payload(text.splitlines(), 'manual')
    _save_lyrics(key, data)
    return data


def _claim_job(key):
    with _jobs_lock:
        if key in _jobs:
            return False
        _jobs.add(key)
        return True


def _alignment_input(data):
    """The lyrics as before their first alignment. Alignments before ALIGN_VERSION 2 replaced
    the LRC timing without keeping it, so it is fetched from LRCLIB again for those."""
    if not (data.get('aligned') and data.get('synced')):
        return data
    lrc = data.get('lrc_segments')
    if lrc is None:
        result = _lrclib(f"get/{data['lrclib_id']}", {}) if data.get('lrclib_id') else None
        lrc = _payload(result, None).get('segments', []) if result else []
    return {**data, 'segments': lrc}


def _align_job(key, vocals_path):
    """Runs a job claimed with _claim_job."""
    try:
        with _align_lock:
            original = load_lyrics(key)
            if not original or not _needs_alignment(original):
                return
            try:
                source = _alignment_input(original)
            except requests.RequestException as e:
                print(f'Lyrics re-alignment for {key} postponed, LRC lookup failed: {e}')
                return
            started = time.time()
            try:
                data = align_lyrics(source, vocals_path, get_device())
                print(f'Aligned lyrics for {key} in {time.time() - started:.1f}s')
            except Exception as e:
                print(f'Lyrics alignment failed for {key}: {e}')
                # An earlier alignment stays in use.
                data = {**original, 'align_error': str(e)}
            current = load_lyrics(key)
            if current and current.get('fetched_at') != data.get('fetched_at'):
                return  # replaced while aligning, e.g. pasted in the admin UI
            _save_lyrics(key, data)
    finally:
        with _jobs_lock:
            _jobs.discard(key)


def ensure_lyrics(key, title=None, artist=None, album=None, duration=None, vocals_path=None, background=True):
    """Fetch lyrics if missing and start word alignment once the vocal stem exists."""
    data = load_lyrics(key)
    retry_missing = data is not None and _lookup_outdated(data)

    if (data is None or retry_missing) and title:
        libretto = librettos.find(title)
        if libretto:
            data = _unsynced_payload(libretto['lines'], 'libretto', work=libretto['work'],
                                     movements=libretto['movements'], required_lines=libretto['required_lines'])
            _save_lyrics(key, data)
        elif artist:
            try:
                result = fetch_lrclib(title, artist, album, duration)
            except requests.RequestException as e:
                print(f'Lyrics lookup failed for {key}: {e}')
                return data
            data = _payload(result, duration) if result else \
                {'status': 'not_found', 'lookup_version': LOOKUP_VERSION, 'fetched_at': time.time()}
            _save_lyrics(key, data)

    if data and _needs_alignment(data) and ALIGN_ENABLED and vocals_path and os.path.isfile(vocals_path):
        if not _claim_job(key):
            return data
        if background:
            threading.Thread(target=_align_job, args=(key, vocals_path), daemon=True).start()
        else:
            _align_job(key, vocals_path)

    return data
