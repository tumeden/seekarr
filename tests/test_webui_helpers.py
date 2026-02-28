from seekarr.webui import _hash_password, _is_newer_version, _parse_semver_tuple, _verify_password


def test_password_hash_roundtrip() -> None:
    password = "correct horse battery staple"
    hashed = _hash_password(password)
    assert hashed.startswith("pbkdf2_sha256$")
    assert _verify_password(password, hashed) is True
    assert _verify_password("wrong-password", hashed) is False


def test_parse_semver_tuple() -> None:
    assert _parse_semver_tuple("v1.2.3") == (1, 2, 3)
    assert _parse_semver_tuple("1.2.3") == (1, 2, 3)
    assert _parse_semver_tuple("1.2") is None
    assert _parse_semver_tuple("invalid") is None


def test_is_newer_version() -> None:
    assert _is_newer_version("v1.2.3", "v1.2.4") is True
    assert _is_newer_version("1.2.3", "1.2.3") is False
    assert _is_newer_version("1.3.0", "1.2.9") is False
