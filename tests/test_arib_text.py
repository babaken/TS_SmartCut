"""ARIB STD-B24 の文字コードを文字列に戻す処理(Rust の native/src/arib.rs、自動カット A1-1)。

バイト列は手で組み立てる。初期状態は G0=漢字、G1=英数、G2=ひらがな、G3=カタカナ、
GL=G0、GR=G2(ARIB STD-B24 第1編 第2部 表7-1 の番組情報の既定)。
"""

import re
from pathlib import Path

import pytest

import ts_smartcut_native as native

decode = native.decode_arib_text
TABLES = Path(__file__).resolve().parents[1] / "native" / "src" / "arib_tables.rs"

ESC = 0x1B
LS0, LS1 = 0x0F, 0x0E
SS2, SS3 = 0x19, 0x1D
MSZ, NSZ = 0x89, 0x8A
CSI = 0x9B


def test_kanji_and_gr_hiragana_by_default():
    # 日本(漢字、GL) + あ(ひらがな、GR の 0xA2)
    assert decode(bytes([0x46, 0x7C, 0x4B, 0x5C, 0xA2])) == "日本あ"


def test_locking_shift_to_alphanumerics_and_back():
    data = bytes([LS1]) + b"ABC" + bytes([LS0, 0x46, 0x7C])
    assert decode(data) == "ABC日"


def test_alphanumeric_yen_and_overline():
    assert decode(bytes([LS1, 0x5C, 0x7E])) == "¥‾"


def test_katakana_via_ls3r_and_hankaku_designation():
    # LS3R(GR=G3=カタカナ) → ア。G1 に JIS X 0201 片仮名を指示して LS1 → ｱ
    data = bytes([ESC, 0x7C, 0xA2, ESC, 0x29, 0x49, LS1, 0x31])
    assert decode(data) == "アｱ"


def test_single_shift_affects_one_character_only():
    # SS2: 次の1文字だけ G2(ひらがな)。その後は G0(漢字)に戻る
    assert decode(bytes([SS2, 0x22, 0x46, 0x7C])) == "あ日"
    # SS3: G3(カタカナ)
    assert decode(bytes([SS3, 0x22, 0x46, 0x7C])) == "ア日"


def test_additional_symbols():
    # G0 に追加記号を指示(ESC $ ;)。[字] [終] は文字列表、範囲内で表にない符号は [#区#点]
    data = bytes([ESC, 0x24, 0x3B, 0x7A, 0x56, 0x7A, 0x6D, 0x7E, 0x7E])
    assert decode(data) == "[字][終][#94#94]"


def test_additional_symbols_in_g1_with_two_byte_designation():
    # ESC $ ) ; : G1 に追加記号。LS1 で GL に呼び出す
    data = bytes([ESC, 0x24, 0x29, 0x3B, LS1, 0x7A, 0x50])
    assert decode(data) == "[HV]"


def test_jis_plane_2_kanji():
    # ESC $ : (JIS 互換漢字2面) → 𠂉(U+20089)
    assert decode(bytes([ESC, 0x24, 0x3A, 0x21, 0x21])) == "\U00020089"


def test_combining_characters_and_variation_selectors():
    # か゜(合成文字)は常に2文字。字形の異なる漢字(櫛・曙)は use_ivs の時だけ異体字セレクタを付ける
    assert decode(bytes([0x24, 0x77])) == "か゚"
    assert decode(bytes([0x36, 0x7B])) == "櫛"
    assert decode(bytes([0x36, 0x7B]), use_ivs=True) == "櫛\U000e0101"
    kigou_akebono = bytes([ESC, 0x24, 0x3B, 0x75, 0x4B])
    assert decode(kigou_akebono) == "曙"
    assert decode(kigou_akebono, use_ivs=True) == "曙\U000e0101"


def test_character_size_is_ignored_by_default():
    # 既定(rplstool-cs と同じ): 文字の大きさの指定で全角・半角を変えない
    data = bytes([MSZ, 0x23, 0x41, 0x21, 0x21, NSZ, LS1, 0x41, 0x20])
    assert decode(data) == "Ａ　A "


def test_character_size_applied():
    # 中型(MSZ)の全角英数は半角、標準(NSZ)の英数・空白は全角
    data = bytes([MSZ, 0x23, 0x41, 0x21, 0x21, NSZ, LS1, 0x41, 0x20])
    assert decode(data, apply_char_size=True) == "A Ａ　"


def test_drcs_becomes_question_mark():
    # 2バイトの外字(ESC $ ( SP @)は「？」、1バイトの外字(ESC ) SP A)は「?」
    assert decode(bytes([ESC, 0x24, 0x28, 0x20, 0x40, 0x21, 0x21])) == "？"
    assert decode(bytes([ESC, 0x29, 0x20, 0x41, LS1, 0x21])) == "?"


def test_default_macro_redesignates():
    # G3 にマクロを指示(ESC + SP p)し、SS3 でマクロ 0x61(G1=カタカナ)を呼ぶ。LS1 でア
    data = bytes([ESC, 0x2B, 0x20, 0x70, SS3, 0x61, LS1, 0x22])
    assert decode(data) == "ア"


def test_newlines_and_csi_are_handled():
    # CR LF / CR / LF はどれも改行1つ。CSI(制御シーケンス)は文字にしない
    data = bytes([0x46, 0x7C, 0x0D, 0x0A, 0x4B, 0x5C, 0x0D, CSI]) + b"1;2 V" + bytes([0x46, 0x7C])
    assert decode(data) == "日\n本\n日"


def test_truncated_data_does_not_raise():
    # 途中で切れたエスケープや2バイト文字の前半だけ
    for data in (bytes([ESC]), bytes([ESC, 0x24]), bytes([ESC, 0x29, 0x20]), bytes([0x46]), bytes([CSI, 0x31])):
        decode(data)


def test_empty():
    assert decode(b"") == ""


# ---- 文字の表(tools/convert_arib_tables.py が作る) ----------------------------

def test_tables_have_expected_sizes():
    # 元の rplsinfo / rplstool-cs の表の要素数(2026-10-07 に変換、両者で一致を確認)
    source = TABLES.read_text(encoding="utf-8")
    sizes = dict(re.findall(r"pub static (\w+): \[[^;]+; (\d+)\]", source))
    assert {k: int(sizes[k]) for k in ("JIS12", "JIS3", "JIS4", "KIGOU1", "KIGOU2_TEXT", "JIS3_COMB_IVS")} == {
        "JIS12": 13758, "JIS3": 3786, "JIS4": 4872, "KIGOU1": 898, "KIGOU2_TEXT": 36, "JIS3_COMB_IVS": 38}
    comb = source[source.index("JIS3_COMB_IVS"):]
    assert set(re.findall(r"^    \((0x[0-9A-F]+),", comb, re.M)) == {"0x39", "0x3B"}


_SOURCES = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(
    not (_SOURCES / "rplstool-cs" / "RplsTool.Core" / "AribTables.cs").exists()
    or not (_SOURCES / "rplsinfo" / "characterSets.cpp").exists(),
    reason="rplstool-cs / rplsinfo のソースが無い",
)
def test_generated_tables_match_the_sources(tmp_path, monkeypatch):
    # 生成し直したものが、置いてある native/src/arib_tables.rs と同じ
    import tools.convert_arib_tables as convert

    monkeypatch.setattr(convert, "OUT", tmp_path / "arib_tables.rs")
    assert convert.main([
        "convert",
        str(_SOURCES / "rplstool-cs" / "RplsTool.Core" / "AribTables.cs"),
        str(_SOURCES / "rplsinfo" / "characterSets.cpp"),
    ]) == 0
    generated = (tmp_path / "arib_tables.rs").read_text(encoding="utf-8")
    current = TABLES.read_text(encoding="utf-8")
    assert re.sub(r"\r\n", "\n", generated) == re.sub(r"\r\n", "\n", current)
