import pytest

from ts_smartcut.errors import LanguageFileNotFoundError
from ts_smartcut.i18n.language_manager import LanguageManager


def test_default_language_is_jp():
    lang = LanguageManager()
    assert lang.lang_code == "jp"
    assert lang.get("menu.file") == "ファイル"


def test_english_language_loads():
    lang = LanguageManager("en")
    assert lang.get("menu.file") == "File"
    assert lang.get("menu.edit") == "Edit"


def test_get_with_dotted_path():
    lang = LanguageManager("jp")
    assert lang.get("buttons.set_start") == "Set Start"
    assert lang.get("messages.encode_title") == "エンコード"


def test_get_missing_key_returns_key_itself():
    lang = LanguageManager("jp")
    assert lang.get("menu.nonexistent") == "menu.nonexistent"
    assert lang.get("totally.unknown.path") == "totally.unknown.path"


def test_get_with_format_kwargs():
    lang = LanguageManager("jp")
    result = lang.get("messages.file_not_found", path="/tmp/video.mp4")
    assert result == "ファイルが見つかりません: /tmp/video.mp4"


def test_get_non_leaf_path_returns_key():
    lang = LanguageManager("jp")
    # "menu"はdictであり文字列ではないので、キーそのものを返す
    assert lang.get("menu") == "menu"


def test_unsupported_language_raises_e402():
    with pytest.raises(LanguageFileNotFoundError) as exc_info:
        LanguageManager("fr")
    assert exc_info.value.code == "E402"


def test_error_messages_cover_full_taxonomy():
    from ts_smartcut.errors import ERROR_DESCRIPTIONS

    for lang_code in ("jp", "en"):
        lang = LanguageManager(lang_code)
        for code in ERROR_DESCRIPTIONS:
            assert lang.get_error_message(code) != code, f"{lang_code}: {code} missing translation"


def test_get_error_message_unknown_code_returns_code():
    lang = LanguageManager("jp")
    assert lang.get_error_message("E000") == "E000"
