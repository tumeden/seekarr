import sqlite3

from seekarr.state import StateStore


def test_arr_api_key_roundtrip_and_clear(tmp_path) -> None:
    store = StateStore(str(tmp_path / "seekarr.db"))
    assert store.has_arr_api_key("radarr", 1) is False
    assert store.get_arr_api_key("radarr", 1) is None

    store.set_arr_api_key("radarr", 1, "super-secret")
    assert store.has_arr_api_key("radarr", 1) is True
    assert store.get_arr_api_key("radarr", 1) == "super-secret"

    store.clear_arr_api_key("radarr", 1)
    assert store.has_arr_api_key("radarr", 1) is False
    assert store.get_arr_api_key("radarr", 1) is None


def test_arr_api_key_corrupt_token_returns_none(tmp_path) -> None:
    db_path = tmp_path / "seekarr.db"
    store = StateStore(str(db_path))
    store.set_arr_api_key("sonarr", 2, "abc123")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE arr_credentials SET api_key_enc = ? WHERE app_type = ? AND instance_id = ?",
            ("not-a-valid-fernet-token", "sonarr", 2),
        )

    # InvalidToken should be handled and return None instead of crashing.
    assert store.get_arr_api_key("sonarr", 2) is None
