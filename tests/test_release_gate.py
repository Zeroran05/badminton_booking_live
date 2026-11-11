from live_booking import (
    is_release_not_open_message,
    is_transient_selection_conflict_message,
)


def test_detects_release_gate_message() -> None:
    assert is_release_not_open_message("次日场地每天 8:00 起开放预约，请 8:00 后再试")


def test_does_not_misclassify_normal_rejection() -> None:
    assert not is_release_not_open_message("该时段已被预约")
    assert not is_release_not_open_message("该场地今天不开放预约")


def test_detects_transient_selection_conflict() -> None:
    assert is_transient_selection_conflict_message("此时间已被选中，请选择其他时间")


def test_does_not_treat_real_occupancy_as_selection_residue() -> None:
    assert not is_transient_selection_conflict_message("该时段已被预约")
