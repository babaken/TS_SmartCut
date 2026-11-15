"""ARIB STD-B24 の文字の表を rplstool-cs / rplsinfo のソースから Rust の表に変換する。

    python tools/convert_arib_tables.py <rplstool-cs の AribTables.cs> <rplsinfo の characterSets.cpp>

出力: native/src/arib_tables.rs(上書き。2026-10-28 までは ts_smartcut/broadcast/arib_tables.py)。表は手で写さない
(写し間違いを防ぐ)。文字に戻すのは Rust の部品(native/src/arib.rs)。

- ほとんどの表は rplstool-cs(rplsinfo の C# 移植)の AribTables.cs から読む。元の
  rplsinfo の characterSets.cpp と数値がすべて一致することを確かめてある(2026-10-07)。
- 組み合わせ文字・異体字の表(jis3CombIvsTable)だけは rplsinfo から読む。元は
  「種類」の欄が F_JIS1KANJI / F_KIGOU という定数名で、rplstool-cs への変換でそれが
  落ちて4つ組がずれていた(F_KIGOU の行は種類が消え、F_JIS1KANJI は 1 になった)ため。
- characterSets.cpp は Shift_JIS(cp932)で書かれている。数値しか使わないので、
  読めない文字は無視する。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "native" / "src" / "arib_tables.rs"

# AribTables.cs の表名 → 出力する名前(Rust の static の名前)
FLAT_TABLES = {
    "Jis12Table": "JIS12",
    "Jis12WinTable": "JIS12_WIN",
    "Jis3Table": "JIS3",
    "Jis4Table": "JIS4",
    "Kigou1Table": "KIGOU1",
    "Hiragana1Table": "HIRAGANA1",
    "Katakana1Table": "KATAKANA1",
    "KanaCommon1Table": "KANA_COMMON1",
    "Hankaku1Table": "HANKAKU1",
    "CharSize1Table": "CHAR_SIZE1",
    "CharSize2Table": "CHAR_SIZE2",
}
# rplsinfo の convToUnicode.h の定義
C_CONSTANTS = {"F_JIS1KANJI": 0x39, "F_KIGOU": 0x3B}


def _numbers(body: str) -> list[int]:
    body = re.sub(r"//[^\n]*", "", body)
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    return [int(x, 16) if x.lower().startswith("0x") else int(x) for x in re.findall(r"\b0x[0-9A-Fa-f]+\b|\b\d+\b", body)]


def _cs_table(source: str, name: str) -> str:
    match = re.search(name + r"\s*=\s*new [^{]*\{(.*?)\n    \};", source, re.S)
    if match is None:
        raise SystemExit(f"AribTables.cs に {name} がありません")
    return match.group(1)


def read_flat_tables(cs_source: str) -> dict[str, list[int]]:
    tables = {}
    for cs_name, py_name in FLAT_TABLES.items():
        values = _numbers(_cs_table(cs_source, cs_name))
        if len(values) % 2:
            raise SystemExit(f"{cs_name} の要素数が奇数です({len(values)})")
        tables[py_name] = values
    return tables


def read_kigou2(cs_source: str) -> dict[int, str]:
    """追加記号の文字列表([HV] [字] 等)。2行で1組: 1行目がJIS、2行目が文字(0で終わり)。"""
    rows = re.findall(r"\{([^{}]*)\}", _cs_table(cs_source, "kigou2TableUTF16"))
    if len(rows) % 2:
        raise SystemExit("kigou2TableUTF16 の行数が奇数です")
    result = {}
    for key_row, text_row in zip(rows[0::2], rows[1::2]):
        key = _numbers(key_row)[0]
        chars = []
        for value in _numbers(text_row):
            if value == 0:
                break
            chars.append(chr(value))
        result[key] = "".join(chars)
    return result


def read_comb_ivs(c_source: str) -> list[tuple[int, int, int, int]]:
    match = re.search(r"jis3CombIvsTable\[\]\s*=\s*\{(.*?)\n\};", c_source, re.S)
    if match is None:
        raise SystemExit("characterSets.cpp に jis3CombIvsTable がありません")
    body = re.sub(r"//[^\n]*", "", match.group(1))
    tokens = [t.strip() for t in body.replace("\n", " ").split(",") if t.strip()]
    values = []
    for token in tokens:
        if token in C_CONSTANTS:
            values.append(C_CONSTANTS[token])
        else:
            values.append(int(token, 0))
    if len(values) % 4:
        raise SystemExit(f"jis3CombIvsTable の要素数が4の倍数ではありません({len(values)})")
    return [tuple(values[i:i + 4]) for i in range(0, len(values), 4)]


def _format_flat(name: str, values: list[int]) -> str:
    lines = [f"pub static {name}: [u32; {len(values)}] = ["]
    for i in range(0, len(values), 8):
        lines.append("    " + ", ".join(f"0x{v:X}" for v in values[i:i + 8]) + ",")
    lines.append("];")
    return "\n".join(lines)


def _rust_str(text: str) -> str:
    return '"' + "".join(c if c.isprintable() and c not in '"\\' else f"\\u{{{ord(c):X}}}" for c in text) + '"'


def render(tables: dict[str, list[int]], kigou2: dict[int, str], comb: list[tuple[int, int, int, int]]) -> str:
    parts = [
        "//! ARIB STD-B24 の文字の表 (自動生成。手で直さない)。",
        "//!",
        "//! tools/convert_arib_tables.py が rplstool-cs の AribTables.cs と rplsinfo の",
        "//! characterSets.cpp から作る。元は rplsinfo (https://github.com/tsukumijima/rplsinfo)。",
        "//! 作者の記載により改変・再配布は自由 (連絡・許可は不要)。",
        "//!",
        "//! 平らな表は (変換元, 変換先) の組を順に並べたもの。同じ変換元が複数あれば最初の組を使う。",
        "",
        "#[rustfmt::skip]",
        "#[allow(dead_code)] // KANA_COMMON1 は使わない (元の表をそのまま持つ)",
        "mod tables {",
    ]
    for name, values in tables.items():
        parts.append(_format_flat(name, values))
        parts.append("")
    parts.append("// 追加記号の文字列 ([HV] [字] [終] 等): JIS → 文字列")
    parts.append(f"pub static KIGOU2_TEXT: [(u32, &str); {len(kigou2)}] = [")
    for key, text in kigou2.items():
        parts.append(f"    (0x{key:X}, {_rust_str(text)}),")
    parts.append("];")
    parts.append("")
    parts.append("// 組み合わせ文字・異体字: (文字の種類 0x39=漢字 / 0x3B=追加記号, JIS, 文字1, 文字2)")
    parts.append(f"pub static JIS3_COMB_IVS: [(u32, u32, u32, u32); {len(comb)}] = [")
    for row in comb:
        parts.append("    (" + ", ".join(f"0x{v:X}" for v in row) + "),")
    parts.append("];")
    parts.append("}")
    parts.append("pub use tables::*;")
    return "\n".join(parts) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    cs_source = Path(argv[1]).read_text(encoding="utf-8")
    c_source = Path(argv[2]).read_text(encoding="cp932", errors="ignore")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(read_flat_tables(cs_source), read_kigou2(cs_source), read_comb_ivs(c_source)), encoding="utf-8")
    print(OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
