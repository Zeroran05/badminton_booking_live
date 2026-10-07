from live_booking import is_release_not_open_message


def test_detects_release_gate_message() -> None:
    assert is_release_not_open_message("次日场地每天 8:00 起开放预约，请 8:00 后再试")


def test_does_not_misclassify_normal_rejection() -> None:
    assert not is_release_not_open_message("该时段已被预约")
    assert not is_release_not_open_message("该场地今天不开放预约")
