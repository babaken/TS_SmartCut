//! ARIB STD-B24 の文字コード (放送の番組名・局名・番組説明) を文字列に戻す。
//!
//! rplstool-cs (RplsTool.Core/AribCodec.cs の Decode) の移植。元は rplsinfo の convToUnicode.cpp
//! (https://github.com/tsukumijima/rplsinfo。作者の記載により改変・再配布は自由)。文字に戻す方向だけ。
//! 2026-10-28 までは ts_smartcut/broadcast/arib_text.py (同じ決まり。tests/test_arib_text.py が確かめる)。
//!
//! 元との違い:
//! - 改行は "\n" (元は "\r\n")。
//! - 組み合わせ文字・異体字 (か゜、曙の異体字 等) の表は rplsinfo のものを使う。rplstool-cs では表の変換で
//!   ずれていて、この表が使われていなかった (tools/convert_arib_tables.py を参照)。
//!
//! 外字 (DRCS) は元と同じく、2 バイトの外字を「？」、1 バイトの外字を「?」にする。

use std::collections::HashMap;
use std::sync::OnceLock;

use crate::arib_tables as t;

// 符号の集合の終端符号 (ARIB STD-B24 第1編 第2部 表7-3)
const F_KANJI: u32 = 0x42;
const F_ALPHA: u32 = 0x4A;
const F_HIRAGANA: u32 = 0x30;
const F_KATAKANA: u32 = 0x31;
const F_MOSAIC_A: u32 = 0x32;
const F_MOSAIC_B: u32 = 0x33;
const F_MOSAIC_C: u32 = 0x34;
const F_MOSAIC_D: u32 = 0x35;
const F_P_ALPHA: u32 = 0x36; // プロポーショナル英数
const F_P_HIRAGANA: u32 = 0x37;
const F_P_KATAKANA: u32 = 0x38;
const F_HANKAKU: u32 = 0x49; // JIS X 0201 片仮名
const F_JIS1_KANJI: u32 = 0x39; // JIS 互換漢字1面
const F_JIS2_KANJI: u32 = 0x3A; // JIS 互換漢字2面
const F_KIGOU: u32 = 0x3B; // 追加記号
const F_DRCS0: u32 = 0x40; // 2 バイトの外字
const F_MACRO: u32 = 0x70;
// 1 バイトの外字 (DRCS-1〜15) とマクロは 0x100 を足して区別する
const F_DRCS1_A: u32 = 0x141;
const F_DRCS15_A: u32 = 0x14F;
const F_MACRO_A: u32 = 0x170;

const G0: usize = 0;
const G1: usize = 1;
const G2: usize = 2;
const G3: usize = 3;
const GL: usize = 0;
const GR: usize = 1;

const IVS_SELECTORS: [u32; 2] = [0xE0100, 0xE0101];

const ONE_BYTE_GSETS: [u32; 11] = [
    F_ALPHA, F_HIRAGANA, F_KATAKANA, F_MOSAIC_A, F_MOSAIC_B, F_MOSAIC_C, F_MOSAIC_D, F_P_ALPHA, F_P_HIRAGANA,
    F_P_KATAKANA, F_HANKAKU,
];
const TWO_BYTE_GSETS: [u32; 4] = [F_KANJI, F_JIS1_KANJI, F_JIS2_KANJI, F_KIGOU];

fn designation_bank(c: u8) -> Option<usize> {
    match c {
        0x28 => Some(G0),
        0x29 => Some(G1),
        0x2A => Some(G2),
        0x2B => Some(G3),
        _ => None,
    }
}

/// 既定のマクロ (ARIB STD-B24 第1編 第2部 表7-20)。G0〜G3 に入れる符号の集合
fn default_macro(code: u32) -> Option<[u32; 4]> {
    let d = F_DRCS1_A;
    Some(match code {
        0x60 => [F_JIS1_KANJI, F_ALPHA, F_HIRAGANA, F_MACRO_A],
        0x61 => [F_JIS1_KANJI, F_KATAKANA, F_HIRAGANA, F_MACRO_A],
        0x62 => [F_JIS1_KANJI, d, F_HIRAGANA, F_MACRO_A],
        0x63 => [F_MOSAIC_A, F_MOSAIC_C, F_MOSAIC_D, F_MACRO_A],
        0x64 => [F_MOSAIC_A, F_MOSAIC_B, F_MOSAIC_D, F_MACRO_A],
        0x65 => [F_MOSAIC_A, d, F_MOSAIC_D, F_MACRO_A],
        0x66 => [d, d + 1, d + 2, F_MACRO_A],
        0x67 => [d + 3, d + 4, d + 5, F_MACRO_A],
        0x68 => [d + 6, d + 7, d + 8, F_MACRO_A],
        0x69 => [d + 9, d + 10, d + 11, F_MACRO_A],
        0x6A => [d + 12, d + 13, d + 14, F_MACRO_A],
        0x6B => [F_JIS1_KANJI, d + 1, F_HIRAGANA, F_MACRO_A],
        0x6C => [F_JIS1_KANJI, d + 2, F_HIRAGANA, F_MACRO_A],
        0x6D => [F_JIS1_KANJI, d + 3, F_HIRAGANA, F_MACRO_A],
        0x6E => [F_KATAKANA, F_HIRAGANA, F_ALPHA, F_MACRO_A],
        0x6F => [F_ALPHA, F_MOSAIC_A, d, F_MACRO_A],
        _ => return None,
    })
}

// ---- 表 -------------------------------------------------------------------

/// (変換元, 変換先) の組の並び → 変換元から引く表 (同じ変換元は最初の組)
fn forward(table: &[u32]) -> HashMap<u32, u32> {
    let mut map = HashMap::new();
    for pair in table.as_chunks::<2>().0 {
        map.entry(pair[0]).or_insert(pair[1]);
    }
    map
}

/// 変換先から引く表 (同じ変換先は最初の組)
fn reverse(table: &[u32]) -> HashMap<u32, u32> {
    let mut map = HashMap::new();
    for pair in table.as_chunks::<2>().0 {
        map.entry(pair[1]).or_insert(pair[0]);
    }
    map
}

struct Tables {
    jis12: HashMap<u32, u32>,
    jis12_reverse: HashMap<u32, u32>,
    jis12_win: HashMap<u32, u32>,
    jis12_win_reverse: HashMap<u32, u32>,
    jis3: HashMap<u32, u32>,
    jis4: HashMap<u32, u32>,
    hiragana: HashMap<u32, u32>,
    katakana: HashMap<u32, u32>,
    hankaku: HashMap<u32, u32>,
    kigou1: HashMap<u32, u32>,
    char_size1: HashMap<u32, u32>,
    char_size1_reverse: HashMap<u32, u32>,
    char_size2: HashMap<u32, u32>,
    char_size2_reverse: HashMap<u32, u32>,
    kigou2_text: HashMap<u32, &'static str>,
    comb_ivs: HashMap<(u32, u32), (u32, u32)>,
}

fn tables() -> &'static Tables {
    static TABLES: OnceLock<Tables> = OnceLock::new();
    TABLES.get_or_init(|| Tables {
        jis12: forward(&t::JIS12),
        jis12_reverse: reverse(&t::JIS12),
        jis12_win: forward(&t::JIS12_WIN),
        jis12_win_reverse: reverse(&t::JIS12_WIN),
        jis3: forward(&t::JIS3),
        jis4: forward(&t::JIS4),
        hiragana: forward(&t::HIRAGANA1),
        katakana: forward(&t::KATAKANA1),
        hankaku: forward(&t::HANKAKU1),
        kigou1: forward(&t::KIGOU1),
        char_size1: forward(&t::CHAR_SIZE1),
        char_size1_reverse: reverse(&t::CHAR_SIZE1),
        char_size2: forward(&t::CHAR_SIZE2),
        char_size2_reverse: reverse(&t::CHAR_SIZE2),
        kigou2_text: t::KIGOU2_TEXT.iter().copied().collect(),
        // 同じ (種類, JIS) は後の行
        comb_ivs: t::JIS3_COMB_IVS.iter().map(|&(kind, jis, uc1, uc2)| ((kind, jis), (uc1, uc2))).collect(),
    })
}

fn get(map: &HashMap<u32, u32>, code: u32) -> u32 {
    map.get(&code).copied().unwrap_or(0)
}

/// 英数 (ASCII 相当)。0x5C は円記号、0x7E はオーバーライン
fn alpha(code: u32) -> u32 {
    match code {
        0x5C => 0x00A5,
        0x7E => 0x203E,
        0x21..=0x7E => code,
        _ => 0,
    }
}

fn jis12(code: u32) -> u32 {
    let t = tables();
    match get(&t.jis12_win, code) {
        0 => get(&t.jis12, code),
        uc => uc,
    }
}

fn jis12_reverse(code: u32) -> u32 {
    let t = tables();
    match get(&t.jis12_win_reverse, code) {
        0 => get(&t.jis12_reverse, code),
        jis => jis,
    }
}

fn kigou2_text(jis: u32, out: &mut String) {
    if let Some(text) = tables().kigou2_text.get(&jis) {
        out.push_str(text);
        return;
    }
    let (high, low) = (jis >> 8, jis & 0xFF);
    if 0x20 < high && high < 0x7F && 0x20 < low && low < 0x7F {
        out.push_str(&format!("[#{:02}#{:02}]", high - 32, low - 32));
    }
}

fn push(out: &mut String, code: u32) {
    // 表の値はどれも文字になる (サロゲートや 0 は無い)
    if let Some(c) = char::from_u32(code) {
        out.push(c);
    }
}

// ---- 状態 -----------------------------------------------------------------

struct State {
    region: [usize; 2], // GL・GR に呼び出している G0〜G3
    bank: [u32; 4],     // G0〜G3 に指示した符号の集合
    single_shift: bool,
    region_gl_backup: usize,
    normal_size: bool, // 標準の大きさ (NSZ) か、中型・小型 (MSZ/SSZ) か
}

fn is_control(c: u8) -> bool {
    c <= 0x20 || (0x7F..=0xA0).contains(&c) || c == 0xFF
}

/// 制御符号を処理し、読んだバイト数を返す
fn change_state(data: &[u8], pos: usize, state: &mut State) -> usize {
    match data[pos] {
        0x89 => state.normal_size = false, // MSZ
        0x8A => state.normal_size = true,  // NSZ
        0x0F => state.region[GL] = G0,      // LS0
        0x0E => state.region[GL] = G1,      // LS1
        c @ (0x19 | 0x1D) => {
            // SS2 / SS3: 次の 1 文字だけ G2 / G3
            state.region_gl_backup = state.region[GL];
            state.region[GL] = if c == 0x19 { G2 } else { G3 };
            state.single_shift = true;
        }
        0x1B => return escape(data, pos, state),
        _ => {}
    }
    1
}

fn escape(data: &[u8], pos: usize, state: &mut State) -> usize {
    let rest = data.len() - pos;
    if rest < 2 {
        return rest;
    }
    let c1 = data[pos + 1];
    // LS2 LS3 LS1R LS2R LS3R
    let shift = match c1 {
        0x6E => Some((GL, G2)),
        0x6F => Some((GL, G3)),
        0x7E => Some((GR, G1)),
        0x7D => Some((GR, G2)),
        0x7C => Some((GR, G3)),
        _ => None,
    };
    if let Some((region, bank)) = shift {
        state.region[region] = bank;
        return 2;
    }
    if designation_bank(c1).is_some() {
        return designate_one_byte(data, pos, state);
    }
    if c1 == 0x24 {
        return designate_two_byte(data, pos, state);
    }
    2
}

fn designate_one_byte(data: &[u8], pos: usize, state: &mut State) -> usize {
    let rest = data.len() - pos;
    if rest < 3 {
        return rest;
    }
    let bank = designation_bank(data[pos + 1]).unwrap();
    let last = data[pos + 2] as u32;
    if ONE_BYTE_GSETS.contains(&last) {
        state.bank[bank] = last;
        return 3;
    }
    if last == 0x20 {
        // DRCS
        if rest >= 4 && ((0x41..=0x4F).contains(&data[pos + 3]) || data[pos + 3] as u32 == F_MACRO) {
            state.bank[bank] = data[pos + 3] as u32 + 0x100;
            return 4;
        }
        return 4.min(rest);
    }
    3
}

fn designate_two_byte(data: &[u8], pos: usize, state: &mut State) -> usize {
    let rest = data.len() - pos;
    if rest < 3 {
        return rest;
    }
    let c2 = data[pos + 2];
    if TWO_BYTE_GSETS.contains(&(c2 as u32)) {
        // ESC $ F: G0 へ
        state.bank[G0] = c2 as u32;
        return 3;
    }
    if c2 == 0x28 {
        // ESC $ ( SP F: G0 へ 2 バイト DRCS
        if rest >= 5 && data[pos + 3] == 0x20 && data[pos + 4] as u32 == F_DRCS0 {
            state.bank[G0] = F_DRCS0;
            return 5;
        }
        return 4.min(rest);
    }
    if let (0x29..=0x2B, Some(bank)) = (c2, designation_bank(c2)) {
        if rest >= 4 && TWO_BYTE_GSETS.contains(&(data[pos + 3] as u32)) {
            state.bank[bank] = data[pos + 3] as u32;
            return 4;
        }
        if rest >= 5 && data[pos + 3] == 0x20 && data[pos + 4] as u32 == F_DRCS0 {
            state.bank[bank] = F_DRCS0;
            return 5;
        }
        return 4.min(rest);
    }
    3
}

/// CSI (制御シーケンス) を読み飛ばし、読んだバイト数を返す。文字には影響しない
fn csi(data: &[u8], pos: usize) -> usize {
    let mut i = pos + 1;
    let mut params = 0;
    while i < data.len() {
        let b = data[i];
        i += 1;
        if (0x30..=0x39).contains(&b) {
            continue;
        }
        if b == 0x3B {
            params += 1;
            if params == 5 {
                break;
            }
            continue;
        }
        if b == 0x20 {
            params += 1;
            continue;
        }
        break; // 終端文字 (0x42〜0x6F) か想定外の文字
    }
    i - pos
}

fn apply_macro(code: u32, state: &mut State) {
    if let Some(banks) = default_macro(code) {
        state.bank = banks;
        state.region = [G0, G2];
    }
}

// ---- 変換 -----------------------------------------------------------------

/// 中型 (MSZ) の全角の英数・記号を半角にする (apply_char_size の時)
fn two_byte_small(jis2: u32) -> u32 {
    let t = tables();
    if jis2 == 0x2121 {
        return 0x20;
    }
    match alpha(get(&t.char_size1_reverse, jis2)) {
        0 => get(&t.hankaku, get(&t.char_size2_reverse, jis2)),
        uc => uc,
    }
}

fn or(a: u32, b: impl FnOnce() -> u32) -> u32 {
    if a != 0 { a } else { b() }
}

/// ARIB STD-B24 の 8 単位符号を文字列に戻す。
///
/// apply_char_size: 文字の大きさの指定 (MSZ/NSZ) に合わせて全角・半角を選ぶ (中型の英数を半角、標準の
///     大きさの英数・半角カナを全角にする)。use_ivs: 字形の異なる漢字 (曙・櫛 等) に異体字セレクタを付ける。
/// 番組情報はどちらもしない (rplstool-cs の既定と同じ)。
pub fn decode(data: &[u8], apply_char_size: bool, use_ivs: bool) -> String {
    let t = tables();
    let mut state = State { region: [G0, G2], bank: [F_JIS1_KANJI, F_ALPHA, F_HIRAGANA, F_KATAKANA],
                            single_shift: false, region_gl_backup: G0, normal_size: true };
    let mut out = String::new();
    let length = data.len();
    let mut pos = 0;

    while pos < length {
        let c = data[pos];
        if is_control(c) {
            match c {
                0x08 | 0x09 => {
                    out.push(c as char);
                    pos += 1;
                }
                0x0A | 0x0D => {
                    out.push('\n');
                    pos += if c == 0x0D && pos + 1 < length && data[pos + 1] == 0x0A { 2 } else { 1 };
                }
                0x20 => {
                    out.push(if apply_char_size && state.normal_size { '\u{3000}' } else { ' ' });
                    pos += 1;
                }
                0x7F => {
                    out.push('\x7f');
                    pos += 1;
                }
                0x9B => pos += csi(data, pos),
                _ => pos += change_state(data, pos, &mut state),
            }
            continue;
        }

        let jis = (c & 0x7F) as u32;
        let jis2 = if pos + 1 < length { (jis << 8) + (data[pos + 1] & 0x7F) as u32 } else { 0 };
        let bank = state.bank[state.region[if c >= 0x80 { GR } else { GL }]];

        if bank == F_KANJI || bank == F_JIS1_KANJI {
            if let Some(&(uc1, uc2)) = t.comb_ivs.get(&(F_JIS1_KANJI, jis2)) {
                push(&mut out, uc1);
                if use_ivs || !IVS_SELECTORS.contains(&uc2) {
                    push(&mut out, uc2);
                }
            } else {
                let small = if apply_char_size && !state.normal_size { two_byte_small(jis2) } else { 0 };
                let uc = or(small, || or(jis12(jis2), || get(&t.jis3, jis2)));
                if uc != 0 {
                    push(&mut out, uc);
                }
            }
            pos += 2;
        } else if bank == F_JIS2_KANJI {
            let uc = get(&t.jis4, jis2);
            if uc != 0 {
                push(&mut out, uc);
            }
            pos += 2;
        } else if bank == F_ALPHA || bank == F_P_ALPHA {
            let uc = if apply_char_size && state.normal_size { jis12(get(&t.char_size1, jis)) } else { alpha(jis) };
            if uc != 0 {
                push(&mut out, uc);
            }
            pos += 1;
        } else if bank == F_HIRAGANA || bank == F_P_HIRAGANA {
            let uc = get(&t.hiragana, jis);
            if uc != 0 {
                push(&mut out, uc);
            }
            pos += 1;
        } else if bank == F_KATAKANA || bank == F_P_KATAKANA {
            let mut uc = get(&t.katakana, jis);
            if apply_char_size && !state.normal_size {
                uc = get(&t.hankaku, get(&t.char_size2_reverse, jis12_reverse(uc)));
            }
            if uc != 0 {
                push(&mut out, uc);
            }
            pos += 1;
        } else if bank == F_HANKAKU {
            let uc = if apply_char_size && state.normal_size {
                jis12(get(&t.char_size2, jis))
            } else {
                get(&t.hankaku, jis)
            };
            if uc != 0 {
                push(&mut out, uc);
            }
            pos += 1;
        } else if bank == F_KIGOU {
            if let Some(&(uc1, uc2)) = t.comb_ivs.get(&(F_KIGOU, jis2)) {
                push(&mut out, uc1);
                if use_ivs {
                    push(&mut out, uc2);
                }
            } else {
                let mut uc = get(&t.kigou1, jis2);
                if uc == 0 && apply_char_size && !state.normal_size {
                    uc = two_byte_small(jis2);
                }
                let uc = or(uc, || jis12(jis2));
                if uc != 0 {
                    push(&mut out, uc);
                } else {
                    kigou2_text(jis2, &mut out);
                }
            }
            pos += 2;
        } else if bank == F_DRCS0 {
            out.push('？'); // 2 バイトの外字は「？」
            pos += 2;
        } else if (F_DRCS1_A..=F_DRCS15_A).contains(&bank) {
            out.push('?'); // 1 バイトの外字
            pos += 1;
        } else if bank == F_MACRO_A {
            apply_macro(jis, &mut state);
            pos += 1;
        } else {
            pos += 1; // モザイク等 (文字にしない)
        }

        if state.single_shift {
            state.region[GL] = state.region_gl_backup;
            state.single_shift = false;
        }
    }
    out
}
