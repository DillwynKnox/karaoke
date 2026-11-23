"""Bundled texts of classical works that LRCLIB does not have.

A recording's track title is mapped to the movements it contains, mainly by the movement's
opening words ("Weihnachtsoratorium, BWV 248, Pt. 1: No. 8, Großer Herr, o starker König"),
and by its number when the title has no usable text.
"""

import difflib
import functools
import json
import os
import re
import unicodedata

LIBRETTO_DIR = os.path.join(os.path.dirname(__file__), 'librettos')
MIN_TEXT_SIMILARITY = 0.75
MIN_TEXT_LENGTH = 8

_KIND = re.compile(
    r'^(?:(?:Solo\s+\w+\s*&\s*)?(?:Chorus|Chor(?:al|ale)?|Coro|Recit(?:ative|ativo)?|Rezitativ|Aria|Arie|Arioso|'
    r'Duett(?:o)?|Terzett(?:o)?|Quartett|Doppelquartett|Sinfonia|Schlu(?:ß|ss)chor|Evangelist)'
    r'(?:\s+(?:und|mit|and|with|&)\s+\w+)*\s*[.,:]?\s*)+', re.I)
_NUMBER = re.compile(r'^(?:No\.|Nr\.)?\s*(\d+[a-z]?|[IVX]+)[.,]?\s+', re.I)


@functools.lru_cache(maxsize=1)
def _works():
    works = []
    for name in sorted(os.listdir(LIBRETTO_DIR)):
        if name.endswith('.json'):
            with open(os.path.join(LIBRETTO_DIR, name)) as f:
                work = json.load(f)
            work['patterns'] = [re.compile(p) for p in work['title_patterns']]
            for movement in work['movements']:
                movement['normalized'] = _normalize(' '.join(movement['lines']))
            works.append(work)
    return works


def _normalize(text):
    text = unicodedata.normalize('NFKD', text.lower().replace('ß', 'ss'))
    text = ''.join(c for c in text if not unicodedata.combining(c))
    return re.sub(r'[\W_]+', ' ', text).strip()


def _pieces(title):
    """Movement parts of a title, e.g. 'No. 26, Aria. Es ist genug! – No. 27, Recit. Siehe'."""
    tail = title.split(':')[-1] if ':' in title else title
    tail = re.sub(r'nn(?=\()', '', tail)  # Deezer sometimes renders line breaks as 'nn'
    return [p for p in re.split(r'\s+[–-]\s+(?=(?:No\.|Nr\.)?\s*\d)', tail) if p.strip()]


def _query(piece):
    """(number, text) of one title piece."""
    quoted = re.findall(r'["„“»]([^"„“«»]{3,})["“”«]', piece)
    piece = re.sub(r'\([^)]*\)|\[[^\]]*\]', ' ', piece).strip()
    number = None
    m = _NUMBER.match(piece)
    if m:
        number = m.group(1)
        piece = piece[m.end():]
    text = quoted[-1] if quoted else _KIND.sub('', piece)
    return number, _normalize(text)


def _by_text(work, text):
    best, best_score = None, 0.0
    for movement in work['movements']:
        opening = movement['normalized'][:len(text)]
        score = difflib.SequenceMatcher(None, text, opening).ratio()
        if score > best_score:
            best, best_score = movement, score
    return best if best_score >= MIN_TEXT_SIMILARITY else None


def _by_number(work, number):
    number = number.lower() if not re.fullmatch(r'[IVX]+', number) else number
    return next((m for m in work['movements'] if m['number'] == number), None) or \
        next((m for m in work['movements'] if re.fullmatch(rf'{re.escape(number)}[a-z]', m['number'])), None)


def _with_parts(work, movement):
    """A matched '2a' (or '2') also covers its later parts 2b, 2c, … which recordings
    usually put on the same track. The alignment may end before sung-out parts."""
    m = re.fullmatch(r'(\d+)([a-z]?)', movement['number'])
    if not m or m.group(2) not in ('', 'a'):
        return [movement]
    movements = work['movements']
    i = movements.index(movement)
    group = [movement]
    for other in movements[i + 1:]:
        if re.fullmatch(rf'{m.group(1)}[b-z]', other['number']):
            group.append(other)
        else:
            break
    return group


def find(title):
    """{'work', 'movements', 'lines'} for a track of a bundled work, or None."""
    work = next((w for w in _works() if any(p.search(title) for p in w['patterns'])), None)
    if not work:
        return None

    movements, named = [], []
    for piece in _pieces(title):
        number, text = _query(piece)
        movement = _by_text(work, text) if len(text) >= MIN_TEXT_LENGTH else None
        if movement is None and number:
            movement = _by_number(work, number)
        if movement:
            named.append(movement)
            movements.extend(m for m in _with_parts(work, movement) if m not in movements)
    if not movements:
        return None
    lines = [line for m in movements for line in m['lines']]
    # Movements named in the title must be sung; their later parts (2b, 2c, …) are optional.
    last_named = max(movements.index(m) for m in named)
    return {
        'work': work['work'],
        'movements': [m['number'] for m in movements],
        'lines': lines,
        'required_lines': sum(len(m['lines']) for m in movements[:last_named + 1]),
    }
