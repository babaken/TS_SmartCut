"""MPEG-2映像のフィールド表示フラグの書き換え (ISO/IEC 13818-2)。

放送のアニメ等は「ソフトテレシネ」で送られる: 24コマ/秒のプログレッシブの
コマに「トップフィールドが先(top_field_first)」「フィールドを繰り返す
(repeat_first_field)」のビットを付け、3:2の60フィールドとして表示させる。
本編中でインターレースのフレームと混在することも多い(CS-Aの実録画で確認)。

ffmpegのMPEG-2エンコーダーはrepeat_first_fieldを書き出せないため、Smart Cutで
再エンコードした部分は元の表示構造を保てない。そこで、元の各コマのフラグを
エンコード後のビットストリームへ書き戻す。対象のビットは規格上の固定位置に
あり、MPEG-2はスタートコード(00 00 01)の出現をデータ中で防いでいるため、
スタートコードを探して位置を特定できる。

前提: Bフレームなし(-bf 0)で符号化し、符号化順=表示順であること。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import List, Optional

_START_CODE = b"\x00\x00\x01"
_PICTURE_START = 0x00
_SEQUENCE_HEADER = 0xB3
_EXTENSION_START = 0xB5
_SEQUENCE_EXTENSION_ID = 1
_PICTURE_CODING_EXTENSION_ID = 8

# frame_rate_code (表6-4)
_FRAME_RATE_CODES = {
    Fraction(24000, 1001): 1,
    Fraction(24, 1): 2,
    Fraction(25, 1): 3,
    Fraction(30000, 1001): 4,
    Fraction(30, 1): 5,
    Fraction(50, 1): 6,
    Fraction(60000, 1001): 7,
    Fraction(60, 1): 8,
}


@dataclass(frozen=True)
class PictureFlags:
    progressive_frame: bool
    top_field_first: bool
    repeat_first_field: bool


def frame_rate_code(fps: float) -> int:
    """fpsに最も近いMPEG-2のframe_rate_codeを返す。"""
    return min(_FRAME_RATE_CODES.items(), key=lambda item: abs(float(item[0]) - fps))[1]


def _start_codes(data: bytes):
    """(スタートコードの位置, コード値)を順に返す。"""
    i = data.find(_START_CODE)
    while 0 <= i and i + 3 < len(data):
        yield i, data[i + 3]
        i = data.find(_START_CODE, i + 3)


def count_pictures(data: bytes) -> int:
    return sum(1 for _, code in _start_codes(data) if code == _PICTURE_START)


def read_picture_flags(data: bytes) -> List[PictureFlags]:
    """各ピクチャ(符号化順)のフラグを読む(検証用)。"""
    flags = []
    for i, code in _start_codes(data):
        if code == _EXTENSION_START and i + 8 < len(data) and data[i + 4] >> 4 == _PICTURE_CODING_EXTENSION_ID:
            flags.append(PictureFlags(
                progressive_frame=bool(data[i + 8] & 0x80),
                top_field_first=bool(data[i + 7] & 0x80),
                repeat_first_field=bool(data[i + 7] & 0x02),
            ))
    return flags


def rewrite_field_flags(
    data: bytes, flags: List[PictureFlags], rate_code: Optional[int] = None
) -> bytes:
    """ピクチャ(符号化順)ごとにフィールド表示フラグを書き換えたビットストリームを返す。

    - シーケンスヘッダー: frame_rate_codeをrate_codeに(指定時)
    - シーケンス拡張: progressive_sequence=0 (フィールド単位の表示にするため)
    - ピクチャ符号化拡張: top_field_first / repeat_first_field / progressive_frame /
      chroma_420_type(4:2:0ではprogressive_frameと同じ値にする規定)

    規格上の制約(progressive_sequence=0では、repeat_first_fieldは
    progressive_frame=1のときだけ許される。progressive_frame=1ならフレーム
    DCTでなければならない)に反する指定はValueErrorにする。
    """
    out = bytearray(data)
    picture_index = 0
    for i, code in _start_codes(data):
        if code == _SEQUENCE_HEADER and rate_code is not None:
            out[i + 7] = (out[i + 7] & 0xF0) | rate_code
        elif code == _EXTENSION_START and i + 8 < len(data):
            extension_id = data[i + 4] >> 4
            if extension_id == _SEQUENCE_EXTENSION_ID:
                out[i + 5] &= ~0x08 & 0xFF  # progressive_sequence = 0
            elif extension_id == _PICTURE_CODING_EXTENSION_ID:
                if picture_index >= len(flags):
                    raise ValueError("the bitstream has more pictures than flags")
                f = flags[picture_index]
                frame_pred_frame_dct = bool(data[i + 7] & 0x40)
                if f.repeat_first_field and not f.progressive_frame:
                    raise ValueError(f"picture {picture_index}: repeat_first_field is only allowed on progressive frames")
                if f.progressive_frame and not frame_pred_frame_dct:
                    raise ValueError(f"picture {picture_index}: a progressive frame must be frame-coded")
                b3 = out[i + 7] & ~(0x80 | 0x02 | 0x01) & 0xFF
                b3 |= (0x80 if f.top_field_first else 0)
                b3 |= (0x02 if f.repeat_first_field else 0)
                b3 |= (0x01 if f.progressive_frame else 0)  # chroma_420_type
                out[i + 7] = b3
                out[i + 8] = (out[i + 8] & 0x7F) | (0x80 if f.progressive_frame else 0)
                picture_index += 1
    if picture_index != len(flags):
        raise ValueError(f"picture count mismatch: {picture_index} in the bitstream, {len(flags)} flags")
    return bytes(out)
