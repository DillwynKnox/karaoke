from karaoke.guest.ratelimit import RateLimiter


def test_limit_per_window():
    limiter = RateLimiter()
    assert all(limiter.allow('add', ('guest:a',), 3, 60) for _ in range(3))
    assert not limiter.allow('add', ('guest:a',), 3, 60)
    assert limiter.allow('add', ('guest:b',), 3, 60)


def test_every_key_has_to_pay():
    limiter = RateLimiter()
    assert limiter.allow('add', ('guest:a', 'ip:1'), 1, 60)
    # A fresh cookie from the same IP does not get past the limit.
    assert not limiter.allow('add', ('guest:b', 'ip:1'), 1, 60)
    # The refused request did not spend the new guest's token.
    assert limiter.allow('add', ('guest:b', 'ip:2'), 1, 60)


def test_zero_limit_blocks():
    assert not RateLimiter().allow('add', ('x',), 0, 60)
