"""出力ファイル管理 (design doc §9)。

元ファイルは常に保護する（上書き禁止・出力先として再利用しない、
design doc §9.4, CLAUDE.md）。出力先に同じ名前のファイルがあれば連番を
付けた別名にし、既存のファイルは上書きしない（上書き確認は設けない、
利用者の要件 2026-10-05）。

バッチ追加では保存名称を毎回尋ねる（2026-10-11 利用者の要望。1本の録画に入った複数の話を
分けて変換する）。初期値は今のパターンの名前で、既存のファイル・バッチの他の項目と同じ名前に
ならないよう連番を付ける。利用者が入れた名前が既存のファイル等と同じなら注意を出して直して
もらう（check_output_path）。

追加の後で同じ名前のファイルができていた時は、変換を始める時も、変換が終わって出力先へ置く時も
連番の名前にする（上書きもエラーもしない、利用者の決定 2026-10-22）。変換は一時フォルダの中で
行い、できあがったファイルを place_output で出力先へ置く。
"""

from __future__ import annotations

import logging
import os
import shutil
import string
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Optional

from ts_smartcut.core.script_output import COMPANION_SUFFIXES, SCRIPT_EXTENSIONS, companion_path
from ts_smartcut.errors import FileIOError, ValidationError
from ts_smartcut.i18n.language_manager import tr
from ts_smartcut.models import AppSettings

logger = logging.getLogger(__name__)

DEFAULT_FILENAME_PATTERN = AppSettings().output_filename_pattern
SCRIPT_SUFFIXES = set(SCRIPT_EXTENSIONS.values())
# 拡張子とみなして元の録画の拡張子に置き換えるもの。ほかの「.」の後ろ(「1.5話」の「.5話」など)は名前の一部
_CONTAINER_SUFFIXES = {".ts", ".m2ts", ".mts", ".m2t", ".tp", ".trp", ".mp4", ".m4v", ".mkv", ".mka", ".mov",
                       ".avi", ".wmv", ".flv", ".webm", ".mpg", ".mpeg", ".vob", ".nut", ".avs", ".vpy"}
# ファイル名パターンで使える置き換え文字 (design doc §9.2)
PATTERN_FIELDS = ("name", "ext", "date", "time")


def validate_filename_pattern(pattern: str) -> Optional[str]:
    """ファイル名パターンの誤りを説明する文字列を返す(正しければNone)。"""
    if not pattern.strip():
        return tr("output_name.pattern_empty")
    bad = [c for c in '/\\:*?"<>|' if c in pattern]
    if bad:
        # / と \ はフォルダの指定になり、残りはWindowsのファイル名に使えない
        return tr("output_name.pattern_bad_char", char=bad[0])
    try:
        fields = [(f, spec, conv) for _, f, spec, conv in string.Formatter().parse(pattern) if f is not None]
    except ValueError:
        return tr("output_name.pattern_braces", open="{", close="}")
    for field, spec, conv in fields:
        if field not in PATTERN_FIELDS:
            return tr("output_name.pattern_unknown_field", field=f"{{{field}}}",
                      fields=", ".join(f"{{{f}}}" for f in PATTERN_FIELDS))
        if spec or conv:
            return tr("output_name.pattern_format", field=f"{{{field}}}")
    if "name" not in [f for f, _, _ in fields]:
        return tr("output_name.pattern_needs_name", field="{name}")
    return None


# Windows でファイル名に使えない名前(拡張子を付けても使えない)
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def validate_output_stem(stem: str) -> Optional[str]:
    """保存名称(拡張子を除く)の誤りを説明する文字列を返す(正しければNone)。"""
    if not stem.strip():
        return tr("output_name.name_empty")
    if any(ord(c) < 32 for c in stem):
        return tr("output_name.name_control")
    bad = [c for c in '/\\:*?"<>|' if c in stem]
    if bad:
        return tr("output_name.name_bad_char", char=bad[0])
    if stem != stem.rstrip(" ."):
        return tr("output_name.name_bad_end")
    if stem.split(".")[0].upper() in _RESERVED_NAMES:
        return tr("output_name.name_reserved", name=stem)
    return None


class OutputManager:
    def __init__(self, settings: AppSettings):
        self.settings = settings

    def generate_output_path(
        self, input_path: Path, user_specified: Optional[Path] = None,
        now: Optional[datetime] = None, reserved: Iterable[Path] = (), extension: Optional[str] = None,
    ) -> Path:
        """出力ファイルパスを決定する。{date}/{time}はnow(既定: 呼び出した時刻=
        そのファイルの処理開始時刻)の日付(YYYYMMDD)・時刻(HHMMSS)。reserved は既存の
        ファイルと同じく避ける名前(バッチの他の項目の出力先)。extension は出力の拡張子(既定: 元の
        録画と同じ。出力形式が AVS／VPY の時は .avs／.vpy、設計書 §6.9)。

        user_specified(保存名称で決めた出力先)に既にファイルがあれば、上書きせず連番の名前にする。"""
        input_path = Path(input_path).resolve()
        reserved = [Path(r) for r in reserved]

        if user_specified:
            user_specified = self._with_source_extension(Path(user_specified), input_path, extension)
            if self._same_path(user_specified, input_path):
                raise ValidationError("E201", key="exceptions.output_is_input")
            return self._free_path(user_specified, input_path, reserved)

        output_dir = (
            Path(self.settings.default_output_dir)
            if self.settings.default_output_dir
            else input_path.parent
        )
        name = input_path.stem
        ext = input_path.suffix
        pattern = self.settings.output_filename_pattern
        error = validate_filename_pattern(pattern)
        if error:
            # 設定画面では保存できないが、手で編集した設定ファイル等に誤りが
            # 残っていてもバッチ処理は止めずに既定のパターンで出力する。
            logger.warning("%s: %s. Using the default pattern %s", error, pattern, DEFAULT_FILENAME_PATTERN)
            pattern = DEFAULT_FILENAME_PATTERN

        now = now or datetime.now()
        filename = pattern.format(name=name, ext=ext, date=now.strftime("%Y%m%d"), time=now.strftime("%H%M%S"))
        output_path = self._with_source_extension(output_dir / filename, input_path, extension)
        return self._free_path(output_path, input_path, reserved)

    @staticmethod
    def _numbered(path: Path) -> Iterator[Path]:
        """path と、重複時に使う連番の名前を順に返す。連番は生成されたファイル名のstemへ付与する
        (design doc §9.2の例: video_cut.mp4 -> video_cut_001.mp4)。
        §9.1のコード片はcounterをパターンの{name}へ差し戻す実装だが、
        それだと "video_001_cut.mp4" になり§9.2の例と食い違うため、
        ここでは§9.2のドキュメント化された期待出力を正とする。"""
        yield path
        counter = 1
        while True:
            yield path.with_name(f"{path.stem}_{counter:03d}{path.suffix}")
            counter += 1

    def _free_path(self, path: Path, input_path: Path, reserved: Iterable[Path]) -> Path:
        return next(p for p in self._numbered(path) if not self._collides(p, input_path, reserved))

    def place_output(self, source: Path, output_path: Path, input_path: Path, reserved: Iterable[Path] = ()) -> Path:
        """一時フォルダで作った出力 source を output_path に置き、置いたパスを返す。その名前が
        もう使われていれば連番の名前にする(既存のファイルは上書きしない)。

        名前は「無い時だけ作る」(open の "x")で確保してから置く。確かめてから置くまでの間に
        他のアプリが同じ名前のファイルを作っても上書きしない(Linux の rename は黙って上書きする)。
        失敗した時に消すのは、ここで確保したファイルだけ。"""
        output_path, input_path = Path(output_path), Path(input_path).resolve()
        reserved = [Path(r) for r in reserved]
        for candidate in self._numbered(output_path):
            if self._collides(candidate, input_path, reserved):
                continue
            try:
                with open(candidate, "xb"):
                    pass
            except FileExistsError:
                continue
            except OSError as e:
                raise FileIOError("E002", key="exceptions.output_write_failed", path=candidate, error=e)
            try:
                try:
                    os.replace(source, candidate)  # 同じドライブなら名前を変えるだけ
                except OSError:
                    # 一時フォルダを別のドライブに設定した時。source は呼び出し側が一時フォルダごと消す
                    shutil.copyfile(source, candidate)
            except OSError as e:
                candidate.unlink(missing_ok=True)
                raise FileIOError("E002", key="exceptions.output_write_failed", path=candidate, error=e)
            except BaseException:
                candidate.unlink(missing_ok=True)
                raise
            if candidate != output_path:
                logger.warning("%s already exists; the output was saved as %s", output_path, candidate.name)
            return candidate
        raise AssertionError("unreachable")  # _numbered は終わらない

    def claim_outputs(self, output_path: Path, input_path: Path, reserved: Iterable[Path] = (),
                      suffixes: Iterable[str] = ()) -> list:
        """スクリプトの出力(設計書 §6.9)の名前を確保する: output_path と、同じ名前に suffixes を付けた
        ファイル(タイムコード・音声)を空のファイルで作り、[output_path, *そのファイル] を返す。どれかの名前が
        もう使われていれば、そろえて連番の名前にする(既存のファイルは上書きしない)。中身は呼び出し側が
        書き、失敗した時は返したファイルを消す。"""
        output_path, input_path = Path(output_path), Path(input_path).resolve()
        reserved = [Path(r) for r in reserved]
        suffixes = list(suffixes)
        for candidate in self._numbered(output_path):
            if self._collides(candidate, input_path, reserved):
                continue
            targets = [candidate] + [companion_path(candidate, suffix) for suffix in suffixes]
            claimed = []
            try:
                for target in targets:
                    with open(target, "xb"):
                        pass
                    claimed.append(target)
            except FileExistsError:
                for target in claimed:
                    target.unlink(missing_ok=True)
                continue
            except OSError as e:
                for target in claimed:
                    target.unlink(missing_ok=True)
                raise FileIOError("E002", key="exceptions.output_write_failed", path=candidate, error=e)
            if candidate != output_path:
                logger.warning("%s already exists; the output was saved as %s", output_path, candidate.name)
            return targets
        raise AssertionError("unreachable")  # _numbered は終わらない

    @staticmethod
    def _with_source_extension(path: Path, input_path: Path, extension: Optional[str] = None) -> Path:
        """出力の拡張子(=コンテナ)は元ファイルと同じに固定する。TS→MP4のような
        コンテナ変換はこのツールの目的外(利用者の要件、2026-10-05)のため、
        ファイル名パターン等で別の拡張子が指定されても元の拡張子にする。スクリプトの出力
        (設計書 §6.9)は extension(.avs／.vpy)にする。"""
        ext = extension or input_path.suffix
        if path.suffix.lower() == ext.lower():
            return path
        if path.suffix.lower() in _CONTAINER_SUFFIXES:
            return path.with_suffix(ext)
        # 名前の「.」の後ろを拡張子として置き換えると、名前が途中で切れた(「Show 1.5 final」→「Show 1.ts」、2026-11-17)
        return path.with_name(path.name + ext)

    def _collides(self, path: Path, input_path: Path, reserved: Iterable[Path] = ()) -> bool:
        if path.suffix.lower() in SCRIPT_SUFFIXES:
            # スクリプトと一緒に書くファイル(タイムコード・音声)の名前もそろえて空いている名前にする。
            # バッチの他の項目のスクリプト(.avs と .vpy)とも、一緒に書くファイルの名前が重ならないようにする
            if any(companion_path(path, s).exists() for s in COMPANION_SUFFIXES):
                return True
            if any(self._same_script_name(path, r) for r in reserved):
                return True
        return path.exists() or self._same_path(path, input_path) or any(self._same_path(path, r) for r in reserved)

    @classmethod
    def same_output(cls, a: Path, b: Path) -> bool:
        """バッチの 2 つの項目の出力先が重なるか(同じファイル、またはスクリプトの一緒に書くファイル)。"""
        return cls._same_path(a, b) or cls._same_script_name(Path(a), Path(b))

    @classmethod
    def _same_script_name(cls, a: Path, b: Path) -> bool:
        """a と b がどちらもスクリプトで、拡張子を除いた名前が同じ(一緒に書くファイルの名前が重なる)。"""
        return (a.suffix.lower() in SCRIPT_SUFFIXES and Path(b).suffix.lower() in SCRIPT_SUFFIXES
                and cls._same_path(a.with_suffix(""), Path(b).with_suffix("")))

    def check_output_path(self, path: Path, input_path: Path, reserved: Iterable[Path] = (),
                          extension: Optional[str] = None) -> Optional[str]:
        """利用者が保存名称で決めた出力先の誤りを説明する文字列(正しければNone)。名前に使えない
        文字・元の録画と同じ・既にあるファイル・reserved(バッチの他の項目の出力先)と同じ。
        extension は出力の拡張子(既定: 元の録画と同じ)。"""
        path, input_path = Path(path), Path(input_path)
        # 名前を空にすると「.ts」になり、Path.stem はそれを拡張子の無い名前とみなす
        ext = extension or input_path.suffix
        stem = path.name[: -len(ext)] if ext and path.name.lower().endswith(ext.lower()) else path.stem
        error = validate_output_stem(stem)
        if error:
            return error
        if self._same_path(path, input_path):
            return tr("output_name.same_as_source")
        if path.exists():
            return tr("output_name.file_exists", name=path.name)
        if ext.lower() in SCRIPT_SUFFIXES:
            for suffix in COMPANION_SUFFIXES:
                if companion_path(path, suffix).exists():
                    return tr("output_name.file_exists", name=companion_path(path, suffix).name)
        if any(self._same_path(path, r) or self._same_script_name(path, r) for r in reserved):
            return tr("output_name.batch_duplicate", name=path.name)
        return None

    @staticmethod
    def _same_path(a: Path, b: Path) -> bool:
        # 存在しないファイルにも安全なPath.resolve()で正規化して比較する。Windows は
        # 大文字・小文字を区別しない(os.path.normcase)
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
