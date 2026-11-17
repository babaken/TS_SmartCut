"""ffmpeg の版による違いの手当て(ここに集める)。

なるべく版の番号では分けず、ffmpeg が実際に出した行や、どの版でも害の無い指定で手当てする。
開発中の版(BtbN の "N-12345-g…")や配布元が手を入れた版は番号が読めず、新しい版も出力が同じなら
そのまま動くため。番号を使うのは起動時の警告(MIN_FFMPEG_MAJOR)だけ。GPU が使えるかは版の番号では
決めず、入っている ffmpeg で実際の処理と同じ指定を試して決める(ffmpeg/gpu.py、2026-11-17 利用者の決定)。

新しい版で何かが壊れたら、ここに手当てを 1 つ足す(どの版で何が起きたか・確かめた日を書く)。
古い版をやめる時は、その版のための手当てを消す。確かめは 6.1・7.1・8.1 の全テスト。
"""

from __future__ import annotations

import re
from fractions import Fraction
from typing import List, Optional

# ---- 必要な版 --------------------------------------------------------------
# README・資料と同じ。5.x 以前は -stats_mux_pre が無く自動カット案の特徴を計算できず、
# 正確なカットもできない(2026-11-16 実測)。それより新しい版の違いは下の手当てで避ける
MIN_FFMPEG_MAJOR = 6

# ---- 進み具合(全版) ------------------------------------------------------
# 進捗は出力したフレーム数(frame=)で測る。time=は-copytsで放送の大きな時刻
# (約21時間)を扱うと00:00:00.00のままになり使えなかった(ffmpeg 9、2026-10-06)。
_FRAME_RE = re.compile(r"frame=\s*(\d+)")

# ---- 6.x: コピーの進み具合 -------------------------------------------------
# ffmpeg 6.x はコピー(-c copy)の時に frame= を出さず、書いた大きさ(size=、単位は 1024 バイト)
# だけを出す(5.1・7.1 以降は frame= を出す、2026-11-16 実測)。その時は大きさから枚数を見積もる
_SIZE_RE = re.compile(r"^L?size=\s*(\d+)\s*(?:kB|KiB)")

# progress_frames が「進捗の行だが枚数は分からない」時に返す値
NO_FRAMES = -1


def progress_frames(line: str, bytes_per_frame: Optional[float] = None) -> Optional[int]:
    """ffmpeg の標準エラー出力の 1 行が進捗の行なら出力済みのフレーム数を返す。
    進捗の行でない時は None、進捗の行でも枚数が分からない時(size= だけで bytes_per_frame
    が無い)は NO_FRAMES。"""
    stripped = line.lstrip()
    match = _FRAME_RE.search(line)
    if match and stripped.startswith("frame="):
        return int(match.group(1))
    size = _SIZE_RE.match(stripped)
    if size:
        if bytes_per_frame and bytes_per_frame > 0:
            return int(int(size.group(1)) * 1024 / bytes_per_frame)
        return NO_FRAMES
    return None


def bytes_per_frame(bitrate: float, fps: float) -> Optional[float]:
    """元の録画の 1 フレームあたりの大きさ(バイト、音声なども含む平均)。frame= を出さない
    ffmpeg(6.x のコピー)の進み具合を、書いた大きさから見積もるのに使う。映像だけのコピーでは
    少し大きめになり、進み具合は少し遅れて段階の終わりで追いつく(表示用なので許容)。"""
    if bitrate <= 0 or fps <= 0:
        return None
    return bitrate / 8 / fps


# ---- 7.x: libx264 の灰色の絵 -----------------------------------------------
def x264_rate_args(fps: float) -> List[str]:
    """setpts で時刻を付け直すと、ffmpeg 7.x はフレームレートを失い、ビットレート指定の
    libx264 が真っ灰色の映像を出した(6.1・8 以降は起きない、2026-11-16 実測)。libx264 に
    フレームレートを直接渡す。6.1・8.1 では正しく分かっている値を重ねるだけで結果は同じ。
    出力の -r は時刻を丸め、どの版でもフレームがずれたので使わない。"""
    rate = Fraction(fps).limit_denominator(1001)
    return ["-x264-params", f"fps={rate.numerator}/{rate.denominator}"]

