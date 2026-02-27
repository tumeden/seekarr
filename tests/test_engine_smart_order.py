from seekarr.engine import _prioritize_cold_start_seasons


def test_prioritize_cold_start_seasons_orders_within_series() -> None:
    grouped = [
        ((101, 3), []),
        ((202, 1), []),
        ((101, 1), []),
        ((101, 2), []),
        ((202, 2), []),
    ]
    out = _prioritize_cold_start_seasons(grouped, {101})
    assert [x[0] for x in out] == [(101, 1), (202, 1), (101, 2), (101, 3), (202, 2)]
