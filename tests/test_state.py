from seekarr.state import StateStore


def test_count_search_actions_for_item(tmp_path) -> None:
    store = StateStore(str(tmp_path / "seekarr.db"))
    assert store.count_search_actions_for_item("sonarr", 1, "season:10:1") == 0

    store.record_search_action("sonarr", 1, "Sonarr", "season:10:1", "Show Season 01 (Pack)")
    store.record_search_action("sonarr", 1, "Sonarr", "season:10:1", "Show Season 01 (Pack)")
    store.record_search_action("sonarr", 1, "Sonarr", "episode:55", "Show S01E01")

    assert store.count_search_actions_for_item("sonarr", 1, "season:10:1") == 2
