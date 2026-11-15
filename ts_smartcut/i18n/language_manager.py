"""多言語対応 (design doc §13)。

言語ファイルは`ts_smartcut/i18n/languages/language_{code}.json`から読む
（design doc本文の相対パス`languages/language_{lang}.json`はCWD依存で
壊れやすいため、このモジュールファイルからの相対パスに変更している）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from ts_smartcut.errors import LanguageFileNotFoundError

if getattr(sys, "frozen", False):
    # PyInstallerでフリーズした実行体では__file__ベースの相対パス解決が
    # 信頼できないため、sys._MEIPASS（同梱データの展開/配置先ルート）から
    # 辿る。.specの`datas`で`ts_smartcut/i18n/languages`をこの構造のまま
    # 同梱することが前提。
    _LANGUAGES_DIR = Path(sys._MEIPASS) / "ts_smartcut" / "i18n" / "languages"  # type: ignore[attr-defined]
else:
    _LANGUAGES_DIR = Path(__file__).parent / "languages"

DEFAULT_LANGUAGE = "jp"


def _find_languages() -> Dict[str, str]:
    """languages フォルダの language_<code>.json から、言語の記号と表示名(ファイルの language_name。
    どの言語の画面でもその言語自身の名前で出す)を作る。言語を足す時はファイルを置くだけでよい。
    既定の言語を先頭に、残りは記号の順。読めないファイルは選べる言語に入れない。"""
    found: Dict[str, str] = {}
    for path in sorted(_LANGUAGES_DIR.glob("language_*.json")):
        code = path.stem[len("language_"):]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        found[code] = str(data.get("language_name") or code)
    ordered = sorted(found, key=lambda c: (c != DEFAULT_LANGUAGE, c))
    return {code: found[code] for code in ordered}


# 選べる言語の記号(設定に保存する値。jp / en …)と、言語を選ぶ欄に出す名前
LANGUAGE_NAMES = _find_languages()
SUPPORTED_LANGUAGES = tuple(LANGUAGE_NAMES)


class LanguageManager:
    def __init__(self, lang_code: str = DEFAULT_LANGUAGE):
        self.lang_code = lang_code
        self._data = self._load_language(lang_code)
        self.translations: Dict[str, Any] = self._data.get("translations", {})

    @staticmethod
    def _load_language(lang_code: str) -> dict:
        lang_file = _LANGUAGES_DIR / f"language_{lang_code}.json"
        if not lang_file.exists():
            raise LanguageFileNotFoundError("E402", f"Language file not found: {lang_file}")
        try:
            return json.loads(lang_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise LanguageFileNotFoundError("E402", f"Language file is not valid JSON: {lang_file}: {e}")

    def get(self, key: str, **kwargs: Any) -> str:
        """ドット区切りのキーパス（例: "menu.file"）で翻訳文字列を取得する。
        未定義のキーはキー自体を返す（design doc §13.2のフォールバック方針）。"""
        value: Any = self.translations
        for part in key.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return key

        if not isinstance(value, str):
            return key
        if kwargs:
            return value.format(**kwargs)
        return value

    def get_error_message(self, code: str) -> str:
        """エラーコードの翻訳文字列を取得する。未定義ならコードをそのまま返す。"""
        key = f"errors.{code}"
        translated = self.get(key)
        return translated if translated != key else code


# ---- アプリ全体で使う言語 ------------------------------------------------
# 画面の部品(バッチリスト・設定の画面等)は MainWindow の lang を受け取らずに tr() で文字を取る。
# MainWindow が起動時に set_current() する。設定されていなければ既定の言語(テストで部品だけを作る時)。

_current: Optional[LanguageManager] = None


def set_current(lang: LanguageManager) -> None:
    global _current
    _current = lang


def current() -> LanguageManager:
    global _current
    if _current is None:
        _current = LanguageManager(DEFAULT_LANGUAGE)
    return _current


def tr(key: str, **kwargs: Any) -> str:
    """今の言語の文字(LanguageManager.get と同じ)。"""
    return current().get(key, **kwargs)


_english: Optional[LanguageManager] = None


def english(key: str, **kwargs: Any) -> str:
    """英語の文字(ログ・例外の log_message 用。言語の設定に関係なく英語)。"""
    global _english
    if _english is None:
        _english = LanguageManager("en")
    return _english.get(key, **kwargs)
