from seekarr.arr import WantedEpisode
from seekarr.engine import _episode_order_key


def test_episode_order_key_sorts_season_episode_ascending() -> None:
    episodes = [
        WantedEpisode(episode_id=3, series_id=1, series_title="x", series_tvdb_id=1, season_number=1, episode_number=3),
        WantedEpisode(episode_id=1, series_id=1, series_title="x", series_tvdb_id=1, season_number=1, episode_number=1),
        WantedEpisode(episode_id=2, series_id=1, series_title="x", series_tvdb_id=1, season_number=1, episode_number=2),
    ]
    ordered = sorted(episodes, key=_episode_order_key)
    assert [e.episode_number for e in ordered] == [1, 2, 3]
