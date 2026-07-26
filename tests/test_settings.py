"""Tests for the machine-global settings store (settings.json)."""

import pytest

from binky import settings


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BINKY_HOME", str(tmp_path))
    yield


def test_defaults_apply_with_no_file() -> None:
    """Zero-config: with no settings.json, every knob is the built-in default."""
    s = settings.load()
    assert s.ram_reserve_pct == 20
    assert s.disk_reserve_gb == 5
    assert s.proxy_port == 80
    assert s.https_port == 443


def test_save_then_load_round_trips() -> None:
    settings.save(settings.Settings(ram_reserve_pct=30, disk_reserve_gb=10, proxy_port=0))
    s = settings.load()
    assert s.ram_reserve_pct == 30
    assert s.disk_reserve_gb == 10
    assert s.proxy_port == 0
    assert s.https_port == 443  # untouched → default


def test_update_is_a_partial_patch() -> None:
    settings.save(settings.Settings(ram_reserve_pct=40))
    s = settings.update(proxy_port=8080)
    assert s.proxy_port == 8080
    assert s.ram_reserve_pct == 40  # the earlier change survives the patch


def test_update_rejects_unknown_keys() -> None:
    with pytest.raises(settings.SettingsError, match="unknown setting"):
        settings.update(max_parallel=6)  # retired knob must not sneak back in


@pytest.mark.parametrize(
    "field, value",
    [
        ("ram_reserve_pct", settings.RESERVE_MIN - 1),
        ("ram_reserve_pct", settings.RESERVE_MAX + 1),
        ("disk_reserve_gb", -1),
        ("disk_reserve_gb", settings.DISK_RESERVE_MAX_GB + 1),
        ("proxy_port", -1),
        ("https_port", 70000),
    ],
)
def test_save_rejects_out_of_range(field, value) -> None:
    bad = settings.Settings(**{field: value})
    with pytest.raises(settings.SettingsError):
        settings.save(bad)


def test_disk_reserve_of_zero_is_allowed() -> None:
    """0 GB is a valid disk reserve — it disables the disk gate entirely."""
    settings.save(settings.Settings(disk_reserve_gb=0))
    assert settings.load().disk_reserve_gb == 0


def test_load_is_forgiving_of_a_corrupt_file() -> None:
    """A daemon must still boot on a garbled store — it just falls back to defaults."""
    (settings.paths.binky_home() / "settings.json").write_text("{not json", encoding="utf-8")
    s = settings.load()
    assert s.ram_reserve_pct == 20  # default, not a crash


def test_load_ignores_bad_values_but_keeps_good_ones() -> None:
    """A partial/typed-wrong file keeps the keys it can and defaults the rest."""
    (settings.paths.binky_home() / "settings.json").write_text(
        '{"ram_reserve_pct": 33, "proxy_port": "nope", "bogus": 1}', encoding="utf-8"
    )
    s = settings.load()
    assert s.ram_reserve_pct == 33  # good int kept
    assert s.proxy_port == 80  # non-int rejected → default
