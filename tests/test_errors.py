import pytest

from ts_smartcut.errors import (
    ERROR_DESCRIPTIONS,
    DiskSpaceError,
    MuxFailedError,
    TSSmartCutError,
    UnknownError,
    ValidationError,
)


def test_all_documented_codes_present():
    expected_codes = (
        [f"E00{i}" for i in range(1, 6)]
        + [f"E10{i}" for i in range(1, 6)]
        + [f"E20{i}" for i in range(1, 5)]
        + [f"E30{i}" for i in range(1, 4)]
        + [f"E40{i}" for i in range(1, 4)]
        + [f"E50{i}" for i in range(1, 4)]
        + ["E999"]
    )
    for code in expected_codes:
        assert code in ERROR_DESCRIPTIONS


def test_error_carries_code_and_default_message():
    err = MuxFailedError("E101")
    assert err.code == "E101"
    assert err.message == "ffmpeg実行エラー"  # 画面は今の言語(既定は日本語)
    assert err.log_message == ERROR_DESCRIPTIONS["E101"] == "ffmpeg execution error"  # ログは英語
    assert str(err) == "[E101] ffmpeg execution error"


def test_error_text_from_the_language_files():
    """設計書 §13.2: 文は項目と値で渡し、画面は今の言語、ログ(str)は英語。"""
    from ts_smartcut.i18n import language_manager

    err = ValidationError("E202", key="exceptions.cut_overlap")
    assert err.message == "他の残す区間と重複しています"
    assert str(err) == "[E202] It overlaps another keep range"
    language_manager.set_current(language_manager.LanguageManager("en"))
    err = MuxFailedError("E101", key="exceptions.picture_count_mismatch", got=3, expected=4)
    assert err.message == err.log_message == "The number of re-encoded pictures does not match the source: 3 != 4"


def test_error_custom_message_overrides_default():
    err = ValidationError("E202", "custom overlap message")
    assert err.message == "custom overlap message"


def test_disk_space_error_is_file_io_error():
    assert issubclass(DiskSpaceError, TSSmartCutError)
    err = DiskSpaceError("E004", "必要: 1.0GB, 利用可能: 0.2GB")
    assert err.code == "E004"


def test_unknown_code_raises_value_error():
    with pytest.raises(ValueError):
        TSSmartCutError("E000")


def test_unknown_error_fixed_code():
    err = UnknownError("boom")
    assert err.code == "E999"
