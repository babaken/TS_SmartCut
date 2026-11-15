"""numpy の .npz の読み書き (numpy を使わない。設計書 §31.9 段階 ⑤、2026-10-30)。

記録 DB の特徴・ロゴの型・ロゴ画像の材料は .npz (np.savez_compressed) で保存してきた。.npz は配列ごとの
.npy を zip にまとめたもの、.npy は見出し (型・形) と値のバイト列なので、同じ形で読み書きできる。DB の形と
版は変えない (2026-10-30 より前に numpy で書いた記録をそのまま読み、ここで書いた記録は numpy でも読める)。

読めるのは、リトルエンディアン・C の順 (fortran_order が False) の packed.py の型だけ (numpy で書いた記録も
そうなっている)。ほかは ValueError。
"""

from __future__ import annotations

import ast
import io
import struct
import zipfile
from typing import Dict, Iterable, Mapping, Optional

from ts_smartcut.autocut.packed import Packed, dtype_code

_MAGIC = b"\x93NUMPY"
_ALIGN = 64


def npy_bytes(values: Packed) -> bytes:
    """.npy (版 1.0) のバイト列。"""
    header = f"{{'descr': '{values.descr}', 'fortran_order': False, 'shape': {tuple(values.shape)!r}, }}"
    length = len(_MAGIC) + 2 + 2 + len(header) + 1
    header += " " * (-length % _ALIGN) + "\n"
    return _MAGIC + b"\x01\x00" + struct.pack("<H", len(header)) + header.encode("latin1") + values.data


def parse_npy(data: bytes) -> Packed:
    """.npy のバイト列を読む。読めない形は ValueError。"""
    if not data.startswith(_MAGIC) or len(data) < 10:
        raise ValueError("not an .npy file")
    major = data[6]
    if major == 1:
        (length,), start = struct.unpack_from("<H", data, 8), 10
    elif major in (2, 3):
        (length,), start = struct.unpack_from("<I", data, 8), 12
    else:
        raise ValueError(f"unsupported .npy version {major}")
    header = ast.literal_eval(data[start:start + length].decode("latin1"))
    if not isinstance(header, dict) or header.get("fortran_order"):
        raise ValueError("unsupported .npy header (Fortran order or not a dict)")
    descr = header["descr"]
    if not isinstance(descr, str) or descr.startswith(">"):
        raise ValueError(f"unsupported dtype: {descr!r}")
    code = dtype_code(descr)
    shape = tuple(header["shape"])
    values = Packed.zeros((0,), code)
    count = 1
    for n in shape:
        count *= n
    body = data[start + length:start + length + count * values.itemsize]
    return Packed(body, code, shape)


def save(arrays: Mapping[str, Packed]) -> bytes:
    """名前 → 値 の .npz (np.savez_compressed と同じく zip の deflate で縮める)。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for name, values in arrays.items():
            archive.writestr(name + ".npy", npy_bytes(values))
    return buf.getvalue()


def load(blob: bytes, names: Optional[Iterable[str]] = None) -> Dict[str, Packed]:
    """.npz の配列 (names を渡すとその名前だけ読む)。"""
    wanted = set(names) if names is not None else None
    result: Dict[str, Packed] = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        for info in archive.infolist():
            if not info.filename.endswith(".npy"):
                continue
            name = info.filename[:-4]
            if wanted is not None and name not in wanted:
                continue
            result[name] = parse_npy(archive.read(info))
    return result


def text(values: Packed) -> str:
    """uint8 で保存した UTF-8 の文字 (meta の JSON)。"""
    return values.data.decode("utf-8")


def from_text(value: str) -> Packed:
    return Packed(value.encode("utf-8"), "u1")
