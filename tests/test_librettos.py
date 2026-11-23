from karaoke.hub import librettos


def test_movement_by_opening_words():
    found = librettos.find('Weihnachtsoratorium, BWV 248, Pt. 1: No. 8, Großer Herr, o starker König')
    assert found['work'] == 'Weihnachtsoratorium'
    assert found['movements'][0] == '8'
    assert found['lines']


def test_other_titles_are_ignored():
    assert librettos.find('Bohemian Rhapsody') is None
