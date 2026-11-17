//! 部分ごとに切り出した H.264 の TS をつなぐ時の、絵の順番 (POC) の書き換え (ffmpeg/smart_cut.py が使う)。
//!
//! 放送の H.264 はキーフレームが IDR でない I のことが多い (テスト動画 2 本、2026-11-17)。IDR でない絵の POC は、
//! デコーダーが直前の参照の絵の POC の下位 (pic_order_cnt_lsb) から求める。前の部分 (再エンコードした Head や、
//! Simple Copy の前のコピー) の後ろにコピーの部分をつなぐと、その I の下位の値によっては I の POC が前の部分より前に
//! なり、ffmpeg のデコーダーは後ろの部分の B フレームを約 20 秒にわたって捨てた (出力が約 150 枚欠けた)。
//!
//! コピーの部分の、最初の IDR までのすべてのスライスの下位に、同じ値を足す (下位の幅で割った余り)。デコーダーが
//! 求める POC どうしの差は変わらないので、部分の中の表示の順番・B フレームの予測は同じまま。足す値は、最初の I が
//! 前の部分の最後の参照の絵の少し後になるように選ぶ。下位の幅は変えないので、スライスの残りのビットはずれない
//! (CABAC でも書ける)。TS のファイルの中のそのバイトだけをその場で書き換える。エミュレーション防止バイトの数が
//! 変わる値は使わない (別の値を選ぶ)。

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::collections::HashMap;
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};

use crate::ts::{self, SectionAssembler, TS_PACKET_SIZE};

const NAL_SLICE: u8 = 1;
const NAL_IDR: u8 = 5;
const NAL_SPS: u8 = 7;
const NAL_PPS: u8 = 8;
/// chroma_format_idc などが SPS にある profile_idc
const HIGH_PROFILES: [u32; 13] = [100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135];
/// スライスの見出しの、下位の値の後ろに残すビット (書き換える範囲がエミュレーション防止の境目に掛からないよう)
const WINDOW_MARGIN_BITS: usize = 16;
/// スライスの見出しを読むために NAL の先頭から取るバイト数
const WINDOW_BYTES: usize = 48;
/// 前の部分の最後の絵と、コピーの部分の最初の I の POC の間 (shift_order)
const ORDER_GAP: u32 = 32;

type Fail = String;

// ---- ビット・NAL ---------------------------------------------------------------------------------

pub fn unescape(data: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(data.len());
    let mut zeros = 0;
    for &b in data {
        if zeros >= 2 && b == 3 {
            zeros = 0;
            continue;
        }
        out.push(b);
        zeros = if b == 0 { zeros + 1 } else { 0 };
    }
    out
}

pub fn escape(data: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(data.len() + 8);
    let mut zeros = 0;
    for &b in data {
        if zeros >= 2 && b <= 3 {
            out.push(3);
            zeros = 0;
        }
        out.push(b);
        zeros = if b == 0 { zeros + 1 } else { 0 };
    }
    out
}

struct Reader<'a> {
    data: &'a [u8],
    pos: usize,
}

impl Reader<'_> {
    fn u(&mut self, count: usize) -> Result<u32, Fail> {
        let mut value = 0u32;
        for _ in 0..count {
            let byte = *self.data.get(self.pos / 8).ok_or("H.264 header ended early")?;
            value = (value << 1) | ((byte >> (7 - self.pos % 8)) & 1) as u32;
            self.pos += 1;
        }
        Ok(value)
    }

    fn ue(&mut self) -> Result<u32, Fail> {
        let mut zeros = 0;
        while self.u(1)? == 0 {
            zeros += 1;
            if zeros > 31 {
                return Err("bad Exp-Golomb code in an H.264 header".into());
            }
        }
        Ok(((1u64 << zeros) - 1 + self.u(zeros)? as u64) as u32)
    }

    fn se(&mut self) -> Result<i32, Fail> {
        let code = self.ue()? as i64;
        Ok(if code & 1 == 1 { (code + 1) / 2 } else { -(code / 2) } as i32)
    }
}

/// data のビット [pos, pos + count) を value にする (同じ幅)
fn write_bits(data: &mut [u8], pos: usize, count: usize, value: u32) {
    for k in 0..count {
        let bit = (value >> (count - 1 - k)) & 1;
        let p = pos + k;
        let mask = 1u8 << (7 - p % 8);
        if bit == 1 {
            data[p / 8] |= mask;
        } else {
            data[p / 8] &= !mask;
        }
    }
}

/// Annex B のデータの NAL の範囲 (開始コードを除く)
pub fn split_nal_units(data: &[u8]) -> Vec<(usize, usize)> {
    let mut starts = Vec::new();
    let mut i = 0;
    while i + 3 <= data.len() {
        if data[i] == 0 && data[i + 1] == 0 && data[i + 2] == 1 {
            starts.push(i + 3);
            i += 3;
        } else {
            i += 1;
        }
    }
    let mut units = Vec::new();
    for (k, &start) in starts.iter().enumerate() {
        let mut end = if k + 1 < starts.len() { starts[k + 1] - 3 } else { data.len() };
        while end > start && data[end - 1] == 0 {
            end -= 1; // 次の開始コードの前の 00
        }
        if end > start {
            units.push((start, end));
        }
    }
    units
}

// ---- SPS・PPS・スライスの見出し -------------------------------------------------------------------

#[derive(Clone, Debug)]
struct Sps {
    separate_colour_plane: bool,
    log2_max_frame_num: usize,
    poc_type: u32,
    log2_max_poc_lsb: usize,
    frame_mbs_only: bool,
}

fn skip_scaling_list(r: &mut Reader, size: usize) -> Result<(), Fail> {
    let (mut last, mut next) = (8i32, 8i32);
    for _ in 0..size {
        if next != 0 {
            next = (last + r.se()? + 256) % 256;
        }
        if next != 0 {
            last = next;
        }
    }
    Ok(())
}

fn parse_sps(rbsp: &[u8]) -> Result<(u32, Sps), Fail> {
    let mut r = Reader { data: rbsp, pos: 8 };
    let profile = r.u(8)?;
    r.u(16)?;
    let id = r.ue()?;
    let mut separate = false;
    if HIGH_PROFILES.contains(&profile) {
        let chroma = r.ue()?;
        if chroma == 3 {
            separate = r.u(1)? == 1;
        }
        r.ue()?;
        r.ue()?;
        r.u(1)?;
        if r.u(1)? == 1 {
            for n in 0..(if chroma != 3 { 8 } else { 12 }) {
                if r.u(1)? == 1 {
                    skip_scaling_list(&mut r, if n < 6 { 16 } else { 64 })?;
                }
            }
        }
    }
    let log2_max_frame_num = r.ue()? as usize + 4;
    let poc_type = r.ue()?;
    let mut log2_max_poc_lsb = 0;
    if poc_type == 0 {
        log2_max_poc_lsb = r.ue()? as usize + 4;
    } else if poc_type == 1 {
        r.u(1)?;
        r.se()?;
        r.se()?;
        for _ in 0..r.ue()? {
            r.se()?;
        }
    }
    r.ue()?;
    r.u(1)?;
    r.ue()?;
    r.ue()?;
    let frame_mbs_only = r.u(1)? == 1;
    Ok((id, Sps { separate_colour_plane: separate, log2_max_frame_num, poc_type, log2_max_poc_lsb, frame_mbs_only }))
}

#[derive(Default)]
struct Parameters {
    sps: HashMap<u32, Sps>,
    pps: HashMap<u32, Sps>,
}

impl Parameters {
    fn read(&mut self, nal_type: u8, rbsp: &[u8]) -> Result<(), Fail> {
        if nal_type == NAL_SPS {
            let (id, sps) = parse_sps(rbsp)?;
            self.sps.insert(id, sps);
        } else if nal_type == NAL_PPS {
            let mut r = Reader { data: rbsp, pos: 8 };
            let pps_id = r.ue()?;
            let sps_id = r.ue()?;
            let sps = self.sps.get(&sps_id).ok_or(format!("H.264 PPS refers to an unknown SPS {sps_id}"))?;
            self.pps.insert(pps_id, sps.clone());
        }
        Ok(())
    }
}

#[derive(Clone, Debug)]
struct Slice {
    first_mb: u32,
    ref_idc: u8,
    idr: bool,
    poc_type: u32,
    width: usize,
    lsb_pos: usize,
    lsb: u32,
}

fn parse_slice(rbsp: &[u8], params: &Parameters) -> Result<Slice, Fail> {
    let mut r = Reader { data: rbsp, pos: 1 };
    let ref_idc = r.u(2)? as u8;
    let nal_type = r.u(5)? as u8;
    let first_mb = r.ue()?;
    r.ue()?;
    let pps_id = r.ue()?;
    let sps = params.pps.get(&pps_id).ok_or(format!("H.264 slice refers to an unknown PPS {pps_id}"))?;
    if sps.separate_colour_plane {
        r.u(2)?;
    }
    r.u(sps.log2_max_frame_num)?;
    if !sps.frame_mbs_only && r.u(1)? == 1 {
        r.u(1)?; // bottom_field_flag
    }
    if nal_type == NAL_IDR {
        r.ue()?;
    }
    let lsb_pos = r.pos;
    let lsb = if sps.poc_type == 0 { r.u(sps.log2_max_poc_lsb)? } else { 0 };
    Ok(Slice {
        first_mb, ref_idc, idr: nal_type == NAL_IDR, poc_type: sps.poc_type,
        width: sps.log2_max_poc_lsb, lsb_pos, lsb,
    })
}

/// 方式 0 の POC の上位 (H.264 8.2.1.1)
fn poc_msb(lsb: u32, prev_lsb: u32, prev_msb: i64, max_lsb: u32) -> i64 {
    if lsb < prev_lsb && prev_lsb - lsb >= max_lsb / 2 {
        prev_msb + max_lsb as i64
    } else if lsb > prev_lsb && lsb - prev_lsb > max_lsb / 2 {
        prev_msb - max_lsb as i64
    } else {
        prev_msb
    }
}

// ---- TS の映像のスライス ------------------------------------------------------------------------

/// TS の中の 1 つのスライスの見出し (NAL の先頭 WINDOW_BYTES バイトまで) とファイルの位置
struct TsSlice {
    slice: Slice,
    /// NAL の先頭からの、エミュレーション防止バイトの付いたバイトと、それぞれのファイルの位置
    escaped: Vec<u8>,
    offsets: Vec<u64>,
}

/// TS の映像 (H.264) の PES を 1 つずつ組み立て、SPS・PPS を読み、スライスを順に callback へ渡す。
/// callback が false を返したらやめる
fn each_slice(file: &mut File, mut callback: impl FnMut(TsSlice) -> bool) -> Result<(), Fail> {
    let mut data = Vec::new();
    file.seek(SeekFrom::Start(0)).map_err(|e| e.to_string())?;
    file.read_to_end(&mut data).map_err(|e| e.to_string())?;
    let (packet_size, start) = ts::detect_packet_size(&data).ok_or("not a TS file")?;
    let offset = packet_size - TS_PACKET_SIZE;

    let mut pat = SectionAssembler::default();
    let mut pmt_pid: Option<u16> = None;
    let mut pmt = SectionAssembler::default();
    let mut video_pid: Option<u16> = None;
    let mut params = Parameters::default();
    // 組み立て中の PES の ES (バイト, ファイルの位置)
    let mut es: Vec<u8> = Vec::new();
    let mut positions: Vec<u64> = Vec::new();
    let mut in_pes = false;

    let mut flush = |es: &[u8], positions: &[u64], params: &mut Parameters| -> Result<bool, Fail> {
        for (s, e) in split_nal_units(es) {
            let nal_type = es[s] & 0x1F;
            match nal_type {
                NAL_SPS | NAL_PPS => params.read(nal_type, &unescape(&es[s..e]))?,
                NAL_SLICE | NAL_IDR => {
                    let end = (s + WINDOW_BYTES).min(e);
                    let mut window_end = end;
                    // 窓の終わりが 00・エミュレーション防止の 03 の直後・その手前に来ないようにする
                    // (作り直すと、窓の外のバイトとの間の 03 の付き方が変わる)
                    while window_end < e
                        && (es[window_end - 1] == 0
                            || es[window_end] == 3
                            || (window_end >= s + 3 && es[window_end - 3..window_end] == [0, 0, 3]))
                    {
                        window_end += 1;
                    }
                    let escaped = es[s..window_end].to_vec();
                    let slice = parse_slice(&unescape(&escaped), params)?;
                    let ts_slice = TsSlice { slice, escaped, offsets: positions[s..window_end].to_vec() };
                    if !callback(ts_slice) {
                        return Ok(false);
                    }
                }
                _ => {}
            }
        }
        Ok(true)
    };

    let mut pos = start;
    while pos + packet_size <= data.len() {
        let p = &data[pos + offset..pos + packet_size];
        let file_pos = (pos + offset) as u64;
        pos += packet_size;
        if p[0] != ts::SYNC_BYTE {
            return Err("lost TS sync in a part file".into());
        }
        let pid = (((p[1] & 0x1F) as u16) << 8) | p[2] as u16;
        if pid == 0 {
            for section in pat.feed(p) {
                if let Some(&(_, pmt)) = ts::parse_pat(&section).first() {
                    pmt_pid = Some(pmt);
                }
            }
            continue;
        }
        if Some(pid) == pmt_pid && video_pid.is_none() {
            for section in pmt.feed(p) {
                let (_, _, streams) = ts::parse_pmt(&section).map_err(|_| "broken PMT")?;
                video_pid = streams.iter().find(|s| s.1 == 0x1B).map(|s| s.0);
            }
            continue;
        }
        if Some(pid) != video_pid {
            continue;
        }
        let afc = (p[3] >> 4) & 0x03;
        if afc & 0x01 == 0 {
            continue;
        }
        let mut payload = 4;
        if afc == 0x03 {
            payload += 1 + p[4] as usize;
        }
        if payload >= TS_PACKET_SIZE {
            continue;
        }
        if p[1] & 0x40 != 0 {
            if in_pes && !flush(&es, &positions, &mut params)? {
                return Ok(());
            }
            es.clear();
            positions.clear();
            // PES の見出し (00 00 01 stream_id, 長さ, 2 バイトのフラグ, 見出しの長さ)
            if payload + 9 > TS_PACKET_SIZE || p[payload..payload + 3] != [0, 0, 1] {
                in_pes = false;
                continue;
            }
            payload += 9 + p[payload + 8] as usize;
            in_pes = true;
        }
        if in_pes && payload < TS_PACKET_SIZE {
            es.extend_from_slice(&p[payload..]);
            positions.extend((payload..TS_PACKET_SIZE).map(|k| file_pos + k as u64));
        }
    }
    if in_pes {
        flush(&es, &positions, &mut params)?;
    }
    if video_pid.is_none() {
        return Err("no H.264 video in the part file".into());
    }
    Ok(())
}

/// 部分ファイルの最後の参照の絵の (下位の幅, 下位の値, 表示がそれより後になる絵の POC の差の最大)。
/// POC の方式 0 でなければ None
fn tail_order(path: &str) -> Result<Option<(usize, u32, i64)>, Fail> {
    let mut file = File::open(path).map_err(|e| e.to_string())?;
    let (mut prev_lsb, mut prev_msb) = (0u32, 0i64);
    let mut last_ref: Option<(usize, u32, i64)> = None; // (幅, 下位, POC)
    let mut max_poc = i64::MIN;
    let mut bad = false;
    each_slice(&mut file, |s| {
        let slice = s.slice;
        if slice.first_mb != 0 {
            return true;
        }
        if slice.poc_type != 0 {
            bad = true;
            return false;
        }
        if slice.idr {
            prev_lsb = 0;
            prev_msb = 0;
            last_ref = None;
            max_poc = i64::MIN;
        }
        let msb = poc_msb(slice.lsb, prev_lsb, prev_msb, 1 << slice.width);
        let poc = msb + slice.lsb as i64;
        max_poc = max_poc.max(poc);
        if slice.ref_idc != 0 {
            prev_lsb = slice.lsb;
            prev_msb = msb;
            last_ref = Some((slice.width, slice.lsb, poc));
        }
        true
    })?;
    if bad {
        return Ok(None);
    }
    Ok(last_ref.map(|(width, lsb, poc)| (width, lsb, max_poc - poc)))
}

/// 部分ファイルの最初の IDR までのスライスの下位に、前の部分に続く値を足す (その場で書き換える)。
/// 足した値を返す。最初の絵が IDR・POC の方式 0 でない時は何もしないで None
fn shift_order(path: &str, prev_lsb: u32, ahead: i64) -> Result<Option<u32>, Fail> {
    let mut file = OpenOptions::new().read(true).write(true).open(path).map_err(|e| e.to_string())?;
    let mut slices: Vec<TsSlice> = Vec::new();
    let mut stop = false;
    each_slice(&mut file, |s| {
        if s.slice.idr && s.slice.first_mb == 0 && !slices.is_empty() {
            return false; // ここから POC は 0 からになる
        }
        if slices.is_empty() && (s.slice.idr || s.slice.poc_type != 0) {
            stop = true;
            return false;
        }
        if s.slice.poc_type != 0 {
            stop = true;
            return false;
        }
        slices.push(s);
        true
    })?;
    if stop || slices.is_empty() {
        return Ok(None);
    }
    let width = slices[0].slice.width;
    if slices.iter().any(|s| s.slice.width != width) {
        return Err("the picture order width changes inside the copied part".into());
    }
    let max_lsb = 1u32 << width;
    if prev_lsb >= max_lsb {
        return Err("the previous part's picture order is wider than the copied part's".into());
    }
    let first = slices[0].slice.lsb;
    // 最初の I の POC が、前の部分の最後の参照の絵より d だけ後 (前の部分の絵はそこから ahead まで後に表示される)
    let lowest = (ahead.max(0) as u64 + 2).min(max_lsb as u64 / 2) as u32;
    // 前の部分の絵とは POC を ORDER_GAP だけ離す。放送の I の後ろの B は I より前の絵を参照することがあり、
    // つなぐとその絵が無く、デコーダーは前の部分の絵で代わりにする。POC が近いとその絵の重みが大きく、
    // 3 枚が元と違う絵になった (間が 2 の時。10 以上で一致、2026-11-17 実録画)
    let start = (lowest + ORDER_GAP).min(max_lsb / 2);
    let candidates = (start..=max_lsb / 2).chain(lowest..start);
    // そのまま作り直して同じバイトにならない窓は、どの値でも書けない
    if let Some(s) = slices.iter().find(|s| escape(&unescape(&s.escaped)) != s.escaped) {
        return Err(format!("an H.264 slice header does not re-escape to the same bytes ({} bytes)", s.escaped.len()));
    }
    'candidates: for d in candidates {
        let add = (prev_lsb + d + max_lsb - first % max_lsb) % max_lsb;
        let mut writes: Vec<(Vec<u8>, &[u64])> = Vec::with_capacity(slices.len());
        for s in &slices {
            let mut rbsp = unescape(&s.escaped);
            if s.slice.lsb_pos + width + WINDOW_MARGIN_BITS > rbsp.len() * 8 {
                return Err("an H.264 slice header is too long to rewrite".into());
            }
            write_bits(&mut rbsp, s.slice.lsb_pos, width, (s.slice.lsb + add) % max_lsb);
            let escaped = escape(&rbsp);
            if escaped.len() != s.escaped.len() {
                continue 'candidates; // エミュレーション防止バイトの数が変わる
            }
            writes.push((escaped, &s.offsets));
        }
        for (bytes, offsets) in writes {
            // 窓のバイトは TS のパケットの境目で分かれていることがある
            let mut k = 0;
            while k < bytes.len() {
                let mut run = 1;
                while k + run < bytes.len() && offsets[k + run] == offsets[k] + run as u64 {
                    run += 1;
                }
                file.seek(SeekFrom::Start(offsets[k])).map_err(|e| e.to_string())?;
                file.write_all(&bytes[k..k + run]).map_err(|e| e.to_string())?;
                k += run;
            }
        }
        file.flush().map_err(|e| e.to_string())?;
        return Ok(Some(add));
    }
    Err("no picture order offset keeps the slice sizes".into())
}

/// 部分ファイル (TS) の最後の参照の絵の (下位の幅, 下位の値, それより後に表示される絵の POC の差の最大)。
/// H.264 の POC の方式 0 でなければ None。読めなければ ValueError
#[pyfunction]
pub fn h264_tail_order(py: Python<'_>, path: String) -> PyResult<Option<(usize, u32, i64)>> {
    py.detach(|| tail_order(&path)).map_err(PyValueError::new_err)
}

/// コピーの部分ファイル (TS) の POC を、前の部分 (h264_tail_order の下位の値と差) に続くよう書き換える。
/// 足した値、書き換える必要が無い (最初の絵が IDR など) 時は None。書き換えられなければ ValueError
#[pyfunction]
pub fn h264_continue_order(py: Python<'_>, path: String, prev_lsb: u32, ahead: i64) -> PyResult<Option<u32>> {
    py.detach(|| shift_order(&path, prev_lsb, ahead)).map_err(PyValueError::new_err)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn escape_round_trip() {
        let raw = [0u8, 0, 0, 1, 0, 0, 3, 7, 0, 0];
        let escaped = escape(&raw);
        assert_eq!(escaped, vec![0, 0, 3, 0, 1, 0, 0, 3, 3, 7, 0, 0]);
        assert_eq!(unescape(&escaped), raw.to_vec());
    }

    #[test]
    fn poc_msb_follows_the_standard() {
        assert_eq!(poc_msb(10, 4090, 0, 4096), 4096);
        assert_eq!(poc_msb(4090, 10, 0, 4096), -4096);
        assert_eq!(poc_msb(2952, 0, 0, 4096), -4096); // Head の後ろの Body の I (実録画)
        assert_eq!(poc_msb(100, 98, 0, 4096), 0);
    }
}
