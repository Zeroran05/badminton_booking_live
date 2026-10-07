from types import SimpleNamespace

import pytest

from main import LIVE_CONFIRMATION, live_authorized
from models import BookingError, BookingStatus


def test_live_requires_config_switch(monkeypatch) -> None:
    monkeypatch.setenv("LIVE_BOOKING_CONFIRM", LIVE_CONFIRMATION)
    with pytest.raises(BookingError) as caught:
        live_authorized(SimpleNamespace(live=True), {"booking": {"live_enabled": False}})
    assert caught.value.status == BookingStatus.LIVE_MODE_NOT_AUTHORIZED


def test_live_requires_env_confirmation(monkeypatch) -> None:
    monkeypatch.delenv("LIVE_BOOKING_CONFIRM", raising=False)
    with pytest.raises(BookingError) as caught:
        live_authorized(SimpleNamespace(live=True), {"booking": {"live_enabled": True}})
    assert caught.value.status == BookingStatus.LIVE_MODE_NOT_AUTHORIZED


def test_live_requires_command_line_switch(monkeypatch) -> None:
    monkeypatch.setenv("LIVE_BOOKING_CONFIRM", LIVE_CONFIRMATION)
    assert live_authorized(SimpleNamespace(live=False), {"booking": {"live_enabled": True}}) is False


def test_live_all_three_gates(monkeypatch) -> None:
    monkeypatch.setenv("LIVE_BOOKING_CONFIRM", LIVE_CONFIRMATION)
    assert live_authorized(SimpleNamespace(live=True), {"booking": {"live_enabled": True}}) is True

