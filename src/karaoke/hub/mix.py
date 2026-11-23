"""Mix of a song: gain of the instrumental, vocal and original track per playback mode."""

# Mix levels are gains in dB relative to the recording: 0 dB plays a track as it is and
# MUTE_DB mutes it. Instrumental and vocals both at 0 dB sound like the original song.
MUTE_DB = -40
MAX_DB = 6
LEVELS = ('instrumental_db', 'vocals_db', 'original_db')
MIX_PRESETS = {
    'karaoke': {'instrumental_db': 0, 'vocals_db': -18, 'original_db': 0},
    'hardcore': {'instrumental_db': 0, 'vocals_db': MUTE_DB, 'original_db': 0},
    # Plays the original track instead of the separated ones.
    'boring': {'instrumental_db': 0, 'vocals_db': 0, 'original_db': 0},
    'drunk': {'instrumental_db': MUTE_DB, 'vocals_db': 0, 'original_db': 0},
}
STEM_LEVELS = ('instrumental_db', 'vocals_db')


def uses_stems(mix):
    return mix['mode'] != 'boring'


def _clamp_db(value):
    return round(min(MAX_DB, max(MUTE_DB, float(value))))


def with_levels(levels, values):
    """Copy of levels with the valid, clamped values applied."""
    return {**levels, **{level: _clamp_db(values[level]) for level in LEVELS if values.get(level) is not None}}


def make_mix(values, presets=MIX_PRESETS):
    """Validated mix from a mode plus optional level overrides. Stem levels that differ
    from the chosen preset turn the mode into 'custom'."""
    mode = values.get('mode') or 'karaoke'
    if mode not in presets and mode != 'custom':
        raise ValueError(f'Unknown mode {mode}')

    mix = with_levels({'mode': mode, **presets['karaoke' if mode == 'custom' else mode]}, values)

    if mode in presets and uses_stems(mix) and \
            any(mix[level] != presets[mode][level] for level in STEM_LEVELS):
        mix['mode'] = 'custom'
    return mix
