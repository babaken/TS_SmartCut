"""数値の並び (numpy の配列の代わり。設計書 §31.9 段階 ⑤、2026-10-30)。

自動カットの計算は Rust の部品が行い、Python は値を受け渡して保存するだけなので、numpy の代わりに
「バイト列 + 型 + 形」だけを持つ小さな入れ物を使う(.exe を小さくするため。numpy は約 28 MB)。
型は numpy の dtype と同じ書き方 (u1・b1・i2・u2・f2・f4・f8・i4・i8。リトルエンディアン)。

- 計算はしない。集計 (count_nonzero・any) と、先頭の軸での取り出し、型の変換 (astype) だけ。
- 保存は npz.py (numpy の .npz と同じ形)。
- テストと確かめのツールは numpy を使ってよい: np.asarray(packed) で numpy の配列になる (__array__)。
"""

from __future__ import annotations

import math
import struct
import sys
from array import array
from math import prod
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple, Union

if sys.byteorder != "little":  # pragma: no cover - 対象の PC (x86・ARM) はリトルエンディアン
    raise ImportError("packed.py expects a little-endian machine")

# 型 → (numpy の descr, memoryview・array の書式, 1 つの大きさ)
_TYPES = {
    "u1": ("|u1", "B", 1),
    "b1": ("|b1", "?", 1),
    "i2": ("<i2", "h", 2),
    "u2": ("<u2", "H", 2),
    "f2": ("<f2", "e", 2),
    "f4": ("<f4", "f", 4),
    "i4": ("<i4", "i", 4),
    "f8": ("<f8", "d", 8),
    "i8": ("<i8", "q", 8),
}
_ALIASES = {"uint8": "u1", "bool": "b1", "int16": "i2", "uint16": "u2", "float16": "f2", "float32": "f4",
            "int32": "i4", "float64": "f8", "int64": "i8"}


def dtype_code(dtype) -> str:
    """'<f4'・'|u1'・'float32' などを 'f4' にする (numpy の dtype・型も受け取る)。ビッグエンディアンや知らない型は
    ValueError。"""
    if not isinstance(dtype, str):
        dtype = getattr(dtype, "str", None) or getattr(dtype, "__name__", None) or str(dtype)
    code = _ALIASES.get(dtype, dtype)
    if code[:1] in "<|=":
        code = code[1:]
    if code not in _TYPES:
        raise ValueError(f"unsupported dtype: {dtype}")
    return code


def as_packed(values, dtype) -> "Packed":
    """Packed にする。テスト・確かめのツールの numpy の配列も受け取る (numpy は読み込まず、配列の astype と
    tobytes を呼ぶだけ)。"""
    if isinstance(values, Packed):
        return values.astype(dtype)
    code = dtype_code(dtype)
    if hasattr(values, "astype") and hasattr(values, "shape"):
        return Packed(values.astype(_TYPES[code][0]).tobytes(), code, tuple(values.shape))
    return Packed.from_values(values, code)


class Packed:
    """C の順 (行優先) に並んだ数値。data は bytes、dtype は型、shape は形。"""

    __slots__ = ("data", "dtype", "shape")

    def __init__(self, data: Union[bytes, bytearray, memoryview], dtype: str, shape: Optional[Sequence[int]] = None):
        self.dtype = dtype_code(dtype)
        self.data = bytes(data)
        size = _TYPES[self.dtype][2]
        if shape is None:
            shape = (len(self.data) // size,)
        self.shape: Tuple[int, ...] = tuple(int(s) for s in shape)
        if prod(self.shape) * size != len(self.data):
            raise ValueError(f"{len(self.data)} bytes do not make shape {self.shape} of {self.dtype}")

    # ---- 作る -------------------------------------------------------------

    @classmethod
    def zeros(cls, shape: Sequence[int], dtype: str) -> "Packed":
        code = dtype_code(dtype)
        return cls(bytes(prod(shape) * _TYPES[code][2]), code, shape)

    @classmethod
    def from_values(cls, values: Iterable, dtype: str, shape: Optional[Sequence[int]] = None) -> "Packed":
        """値の並び (先頭の軸から順に平らにしたもの) から。float16 は最も近い偶数に丸める。"""
        code = dtype_code(dtype)
        values = list(values)
        if code == "f2":
            try:
                data = struct.pack(f"<{len(values)}e", *values)
            except OverflowError:  # numpy と同じく、float16 に入らない大きさは ±inf
                data = b"".join(struct.pack("<e", v if abs(v) < 65520.0 else math.copysign(math.inf, v))
                                for v in values)
        elif code == "b1":
            data = bytes(1 if v else 0 for v in values)
        else:
            data = array(_TYPES[code][1], values).tobytes()
        return cls(data, code, shape if shape is not None else (len(values),))

    # ---- 形 ---------------------------------------------------------------

    @property
    def descr(self) -> str:
        return _TYPES[self.dtype][0]

    @property
    def itemsize(self) -> int:
        return _TYPES[self.dtype][2]

    @property
    def ndim(self) -> int:
        return len(self.shape)

    @property
    def size(self) -> int:
        return prod(self.shape)

    def reshape(self, *shape: int) -> "Packed":
        if len(shape) == 1 and isinstance(shape[0], (tuple, list)):
            shape = tuple(shape[0])
        return Packed(self.data, self.dtype, shape)

    # ---- 読む -------------------------------------------------------------

    def flat(self) -> List:
        """全部の値 (平らにしたもの)。"""
        if self.dtype == "b1":
            return [b != 0 for b in self.data]
        return memoryview(self.data).cast(_TYPES[self.dtype][1]).tolist()

    def tolist(self):
        """入れ子のリスト (numpy の tolist と同じ形)。0 次元は値 1 つ。"""
        values = self.flat()
        if not self.shape:
            return values[0]
        for n in reversed(self.shape[1:]):
            values = [values[k:k + n] for k in range(0, len(values), n)] if n else [[] for _ in range(len(values))]
        return values

    def item(self):
        """値が 1 つの時のその値。"""
        if self.size != 1:
            raise ValueError("item() needs exactly one value")
        return self.flat()[0]

    def tobytes(self) -> bytes:
        return self.data

    def __len__(self) -> int:
        if not self.shape:
            raise TypeError("len() of a 0-d array")
        return self.shape[0]

    def __getitem__(self, index):
        """先頭の軸の 1 つ (多次元なら下の形の Packed、1 次元なら値)、先頭の軸のスライス (Packed)。全部の軸の
        タプルなら値。"""
        if isinstance(index, tuple):
            if len(index) != self.ndim:
                raise IndexError("give an index for every axis")
            offset = 0
            for i, n in zip(index, self.shape):
                i = i + n if i < 0 else i
                if not 0 <= i < n:
                    raise IndexError("index out of range")
                offset = offset * n + i
            return self._value(offset)
        n = len(self)
        if isinstance(index, slice):
            rest = prod(self.shape[1:]) * self.itemsize
            rows = range(n)[index]
            if rows.step == 1:
                data = self.data[rows.start * rest:rows.stop * rest] if len(rows) else b""
            else:
                data = b"".join(self.data[r * rest:(r + 1) * rest] for r in rows)
            return Packed(data, self.dtype, (len(rows),) + self.shape[1:])
        i = index + n if index < 0 else index
        if not 0 <= i < n:
            raise IndexError("index out of range")
        if self.ndim == 1:
            return self._value(i)
        rest = prod(self.shape[1:]) * self.itemsize
        return Packed(self.data[i * rest:(i + 1) * rest], self.dtype, self.shape[1:])

    def _value(self, k: int):
        size = self.itemsize
        piece = self.data[k * size:(k + 1) * size]
        if self.dtype == "b1":
            return piece != b"\0"
        return struct.unpack("<" + _TYPES[self.dtype][1], piece)[0]

    def __iter__(self) -> Iterator:
        for i in range(len(self)):
            yield self[i]

    def count_nonzero(self) -> int:
        if self.itemsize == 1:
            return len(self.data) - self.data.count(0)
        return sum(1 for v in self.flat() if v != 0)

    def any(self) -> bool:
        return self.count_nonzero() > 0

    def astype(self, dtype: str) -> "Packed":
        code = dtype_code(dtype)
        if code == self.dtype:
            return self
        return Packed.from_values(self.flat(), code, self.shape)

    def __eq__(self, other) -> bool:
        return (isinstance(other, Packed) and self.dtype == other.dtype and self.shape == other.shape
                and self.data == other.data)

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"Packed(dtype={self.dtype}, shape={self.shape})"

    def __array__(self, dtype=None, copy=None):
        """numpy の配列 (テスト・確かめのツール用。アプリは numpy を読み込まない)。"""
        import numpy as np

        values = np.frombuffer(self.data, self.descr).reshape(self.shape)
        return values.astype(dtype) if dtype is not None else values.copy()
