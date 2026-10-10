from pathlib import Path

import pytest

import ui_server


def test_default_settings_match_booking_config() -> None:
    settings = ui_server.default_settings()
    assert settings["release_delay_seconds"] == 70
    assert settings["wake_enabled"] is False
    assert settings["venue_priority"] == [2, 3, 1, 4, 5, 6]
    assert [group["name"] for group in settings["time_groups"]] == [
        "afternoon",
        "evening_early",
        "evening_late",
        "morning",
    ]


def test_validate_settings_preserves_order_and_enabled_state() -> None:
    settings = ui_server.default_settings()
    settings["venue_priority"] = [6, 5, 4, 3, 2, 1]
    settings["time_groups"].reverse()
    settings["time_groups"][0]["enabled"] = False
    validated = ui_server.validate_settings(settings)
    assert validated["venue_priority"] == [6, 5, 4, 3, 2, 1]
    assert validated["time_groups"][0]["name"] == "morning"
    assert validated["time_groups"][0]["enabled"] is False


def test_validate_settings_requires_one_time_group() -> None:
    settings = ui_server.default_settings()
    for group in settings["time_groups"]:
        group["enabled"] = False
    with pytest.raises(ui_server.SettingsError, match="至少选择一个"):
        ui_server.validate_settings(settings)


def test_validate_settings_accepts_custom_consecutive_window() -> None:
    settings = ui_server.default_settings()
    settings["time_groups"].append(
        {
            "name": "custom_17_19",
            "slots": ["17:00-18:00", "18:00-19:00"],
            "enabled": True,
            "custom": True,
        }
    )
    validated = ui_server.validate_settings(settings)
    assert validated["time_groups"][-1] == {
        "name": "custom_17_19",
        "label": "17:00–19:00",
        "slots": ["17:00-18:00", "18:00-19:00"],
        "enabled": True,
        "custom": True,
    }


def test_validate_settings_rejects_non_consecutive_window() -> None:
    settings = ui_server.default_settings()
    settings["time_groups"].append(
        {
            "name": "custom_bad",
            "slots": ["17:00-18:00", "19:00-20:00"],
            "enabled": True,
        }
    )
    with pytest.raises(ui_server.SettingsError, match="连续两小时"):
        ui_server.validate_settings(settings)


def test_write_effective_config_does_not_modify_base_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ui_server, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(ui_server, "GENERATED_CONFIG_PATH", tmp_path / "ui_config.yaml")
    base_path = Path(ui_server.ROOT / "config.yaml")
    original = base_path.read_text(encoding="utf-8")
    settings = ui_server.default_settings()
    settings["venue_priority"] = [6, 5, 4, 3, 2, 1]
    settings["time_groups"][0]["enabled"] = False
    generated = ui_server.write_effective_config(settings)
    config = ui_server.yaml.safe_load(generated.read_text(encoding="utf-8"))
    assert config["booking"]["venue_priority"] == [6, 5, 4, 3, 2, 1]
    assert [group["name"] for group in config["booking"]["time_groups"]] == [
        "evening_early",
        "evening_late",
        "morning",
    ]
    assert base_path.read_text(encoding="utf-8") == original


def test_build_command_is_dry_run_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ui_server, "write_effective_config", lambda settings: tmp_path / "ui.yaml")
    monkeypatch.setattr(ui_server.sys, "platform", "test")
    command = ui_server.build_command(ui_server.default_settings(), live=False)
    assert "--run-now" in command
    assert "--once" in command
    assert "--live" not in command


def test_configure_macos_wake_uses_daily_0757(monkeypatch) -> None:
    calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(ui_server.sys, "platform", "darwin")
    monkeypatch.setattr(
        ui_server.subprocess,
        "run",
        lambda command, **kwargs: calls.append(command) or Result(),
    )
    ui_server.configure_macos_wake(True)
    assert "wakeorpoweron MTWRFSU 07:57:00" in calls[0][-1]


def test_configure_macos_wake_rejects_other_platforms(monkeypatch) -> None:
    monkeypatch.setattr(ui_server.sys, "platform", "win32")
    with pytest.raises(ui_server.SettingsError, match="仅支持 macOS"):
        ui_server.configure_macos_wake(True)
