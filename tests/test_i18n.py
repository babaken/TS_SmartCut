"""文字の一覧(language_jp.json / language_en.json)の見張り (設計書 §13.5)。

- 日本語と英語の一覧の項目・{…} の名前が同じ。
- ソースで使っている項目がすべて一覧にある。
- 画面用の文字・ログの文に日本語を直接書いていない(AST で調べる。docstring とコメントは除く)。
  まだ移していないファイルは除外の一覧に入れ、作業(L2〜L8)ごとに減らす。L7 で空にする。
"""

import ast
import json
import re
import string
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "ts_smartcut"
LANGUAGES = PACKAGE / "i18n" / "languages"
JAPANESE = re.compile(r"[぀-ヿ㐀-鿿！-｠]")

# 画面用の文字に日本語が残っているファイル(移したら消す)。
UI_NOT_MIGRATED = {
}
# 画面用ではない日本語(ずっと残す)。
UI_ALLOWED = {
    "autocut/evaluate.py",  # 開発用の評価ツール(tools/autocut_eval.py)の表示
    "autocut/history_db.py",  # SQL の中のコメント
}
# ログの文に日本語が残っているファイル(L8 で消す)。
LOGS_NOT_MIGRATED = {
}


def _load(code: str) -> dict:
    return json.loads((LANGUAGES / f"language_{code}.json").read_text(encoding="utf-8"))["translations"]


def _flatten(tree: dict, prefix: str = "") -> dict:
    out = {}
    for key, value in tree.items():
        if isinstance(value, dict):
            out.update(_flatten(value, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = value
    return out


def _fields(text: str) -> set:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


JP = _flatten(_load("jp"))
# 日本語以外の言語ファイル(足した言語も自動で調べる)
OTHERS = sorted(p.stem[len("language_"):] for p in LANGUAGES.glob("language_*.json") if p.stem != "language_jp")


def test_language_files_are_found_with_their_names():
    """言語はファイルを置くだけで選べる(2026-10-13)。記号はファイル名、名前は language_name。"""
    from ts_smartcut.i18n.language_manager import LANGUAGE_NAMES, SUPPORTED_LANGUAGES

    assert SUPPORTED_LANGUAGES[0] == "jp" and set(SUPPORTED_LANGUAGES) == {"jp", *OTHERS}
    assert LANGUAGE_NAMES["jp"] == "日本語" and LANGUAGE_NAMES["en"] == "English"
    for code in SUPPORTED_LANGUAGES:
        data = json.loads((LANGUAGES / f"language_{code}.json").read_text(encoding="utf-8"))
        assert data["language"] == code and data["language_name"]


@pytest.mark.parametrize("code", OTHERS)
def test_every_language_has_the_same_keys(code):
    assert sorted(set(JP) ^ set(_flatten(_load(code)))) == []


@pytest.mark.parametrize("code", OTHERS)
def test_placeholders_match(code):
    other = _flatten(_load(code))
    wrong = [k for k in JP if k in other and _fields(JP[k]) != _fields(other[k])]
    assert wrong == []


def test_english_has_no_japanese():
    assert [k for k, v in _flatten(_load("en")).items() if JAPANESE.search(v)] == []


def _sources():
    for path in sorted(PACKAGE.rglob("*.py")):
        yield path.relative_to(PACKAGE).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def _is_lookup(call: ast.Call) -> bool:
    """文字の一覧を引く呼び出し: tr("…")・t("…")(t = self.lang.get)・lang.get("…")・self.lang.get("…")。"""
    f = call.func
    if isinstance(f, ast.Name):
        return f.id in ("tr", "t")
    if isinstance(f, ast.Attribute) and f.attr == "get":
        v = f.value
        return (isinstance(v, ast.Name) and v.id == "lang") or (isinstance(v, ast.Attribute) and v.attr == "lang")
    return False


def test_keys_used_in_the_source_exist():
    missing = []
    for name, tree in _sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_lookup(node) and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and "." in arg.value:
                    if arg.value not in JP:
                        missing.append(f"{name}:{node.lineno} {arg.value}")
    assert missing == []


_LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}


def _is_logging(call: ast.Call) -> bool:
    """logger.info(…)・logging.warning(…)・logging.getLogger(…).info(…)。"""
    f = call.func
    if not isinstance(f, ast.Attribute) or f.attr not in _LOG_METHODS:
        return False
    v = f.value
    if isinstance(v, ast.Name):
        return v.id in ("logger", "logging")
    return isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr == "getLogger"


def _japanese_strings(tree: ast.AST):
    """(ログの文か, 行) の組。docstring は除く。"""
    docstrings = set()
    logging_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
        if isinstance(node, ast.Call) and _is_logging(node):
            logging_nodes.update(id(sub) for sub in ast.walk(node))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
                and JAPANESE.search(node.value)):
            yield id(node) in logging_nodes, node.lineno


def test_no_japanese_written_directly_in_the_screens():
    """画面に出る文字(画面の部品・例外の文・保存名称の注意等)は文字の一覧から取る。"""
    found = []
    for name, tree in _sources():
        if name in UI_NOT_MIGRATED or name in UI_ALLOWED:
            continue
        found += [f"{name}:{line}" for is_log, line in _japanese_strings(tree) if not is_log]
    assert found == []


def test_no_japanese_in_log_messages():
    found = []
    for name, tree in _sources():
        if name in LOGS_NOT_MIGRATED:
            continue
        found += [f"{name}:{line}" for is_log, line in _japanese_strings(tree) if is_log]
    assert found == []


@pytest.mark.parametrize("names, kind", [(UI_NOT_MIGRATED, "ui"), (LOGS_NOT_MIGRATED, "log")])
def test_exclusions_are_still_needed(names, kind):
    """除外の一覧に、もう日本語が無いファイルが残っていない(移したら一覧から消す)。"""
    trees = dict(_sources())
    stale = [n for n in sorted(names)
             if not any(is_log == (kind == "log") for is_log, _ in _japanese_strings(trees[n]))]
    assert stale == []
