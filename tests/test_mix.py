import pytest

from karaoke.hub.mix import MAX_DB, MIX_PRESETS, MUTE_DB, make_mix


def test_preset_is_used_as_is():
    assert make_mix({'mode': 'hardcore'}) == {'mode': 'hardcore', **MIX_PRESETS['hardcore']}


def test_default_mode_is_karaoke():
    assert make_mix({})['mode'] == 'karaoke'


def test_changed_stem_level_turns_into_custom():
    assert make_mix({'mode': 'karaoke', 'vocals_db': -6})['mode'] == 'custom'


def test_original_level_does_not_make_custom():
    assert make_mix({'mode': 'boring', 'original_db': -3}) == {
        'mode': 'boring', 'instrumental_db': 0, 'vocals_db': 0, 'original_db': -3}


def test_levels_are_clamped():
    mix = make_mix({'mode': 'custom', 'vocals_db': 99, 'instrumental_db': -99})
    assert mix['vocals_db'] == MAX_DB
    assert mix['instrumental_db'] == MUTE_DB


def test_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        make_mix({'mode': 'opera'})
