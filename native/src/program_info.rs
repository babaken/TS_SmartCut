//! 録画した TS の番組情報 (SI) を読む (設計書 §31.2、自動カット計画 A1)。
//!
//! 読むもの: PAT / PMT (サービス、PCR の PID、映像・音声・字幕の構成)、SDT (局名・事業者名・original_network_id)、
//! EIT (現在・次の番組。番組名・開始時刻・長さ・ジャンル・説明・詳細・音声)、TOT / TDT (放送の現在時刻)。
//! ファイルの先頭・中ほど・末尾の数十 MB だけを読む。
//!
//! 番組情報の解析は rplstool-cs (RplsTool.Core/TsProgramInfoReader.cs) の移植。元は rplsinfo
//! (https://github.com/tsukumijima/rplsinfo。作者の記載により改変・再配布は自由)。
//! 2026-10-28 までは ts_smartcut/broadcast/program_info.py (決まりは同じ)。録画した番組を選ぶ・録画の
//! 時刻を組み立てるのは Python (program_info.read_program_info) のまま (日時の計算を Python と同じにするため)。
//!
//! 壊れたセクション: 決まった位置のバイトが無いセクションは、そのセクションだけ捨てる (元の Python では
//! IndexError)。範囲を指定した切り出しは、Python のスライスと同じく、ある所までにする。

use std::collections::HashMap;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

use crate::arib;
use crate::ts::{OutOfRange, SectionAssembler, Stream, VIDEO_STREAM_TYPES, at, detect_packet_size, length12,
                packet_array, parse_pat, parse_pmt};

const PID_PAT: u16 = 0x0000;
const PID_SDT: u16 = 0x0011;
const PID_EIT: u16 = 0x0012;
const PID_TOT: u16 = 0x0014;

const TABLE_PAT: u8 = 0x00;
const TABLE_PMT: u8 = 0x02;
const TABLE_SDT_ACTUAL: u8 = 0x42;
const TABLE_EIT_PF_ACTUAL: u8 = 0x4E;
const TABLE_TDT: u8 = 0x70;
const TABLE_TOT: u8 = 0x73;

const PCR_CLOCK: i64 = 27_000_000;
const PCR_WRAP: i64 = (1 << 33) * 300;

// 読む量。TOT は最長で 30 秒ごとなので、先頭・末尾は見つかるまで広げる
const CHUNK_SIZE: u64 = 32 * 1024 * 1024;
const MAX_EDGE_SIZE: u64 = 160 * 1024 * 1024;
const MIDDLE_SIZE: u64 = 16 * 1024 * 1024;

/// MJD と時・分・秒 (BCD を戻した値。日時にするのは Python)
pub type Time = (u16, u8, u8, u8);
/// 時・分・秒 (BCD を戻した値)
pub type Duration = (u8, u8, u8);

/// EIT の音声コンポーネント記述子 (0xC4): (component_tag, component_type, stream_type, 主音声, 言語, 言語2, 説明)
pub type AudioComponent = (u8, u8, u8, bool, String, String, String);

pub struct Event {
    pub event_id: u16,
    pub start: Option<Time>,
    pub duration: Option<Duration>,
    pub name: String,
    pub description: String,
    pub extended: String,
    pub genres: Vec<u8>,
    pub audio_components: Vec<AudioComponent>,
}

pub struct Eit {
    pub service_id: u16,
    pub transport_stream_id: u16,
    pub original_network_id: u16,
    pub section_number: u8,
    pub events: Vec<Event>,
}

#[derive(Clone)]
pub struct SdtService {
    pub transport_stream_id: u16,
    pub original_network_id: u16,
    pub provider_name: String,
    pub service_name: String,
}

/// 時刻と、その時刻を読んだ位置の、録画の先頭からの秒数 (PCR による。分からなければ None)
pub type TimeMark = (Time, Option<f64>);

pub struct ProgramInfo {
    pub packet_size: usize,
    pub service_id: u16,
    pub pcr_pid: Option<u16>,
    pub streams: Vec<Stream>,
    /// ファイルの中ほどの PMT (先頭だけで読み終えた短い録画では None)
    pub programme_streams: Option<Vec<Stream>>,
    pub sdt: Option<SdtService>,
    pub first: Option<TimeMark>,
    pub last: Option<TimeMark>,
    pub end_offset: Option<f64>,
    pub head_eits: Vec<Eit>,
    pub middle_eits: Vec<Eit>,
    pub tail_eits: Vec<Eit>,
}

// ---- 値の読み方 -----------------------------------------------------------

fn bcd(value: u8) -> u8 {
    (value >> 4) * 10 + (value & 0x0F)
}

/// Python のスライス data[a:b] と同じ (範囲の外はある所まで)
fn slice(data: &[u8], a: usize, b: usize) -> &[u8] {
    let s = a.min(data.len());
    let e = b.min(data.len()).max(s);
    &data[s..e]
}

/// MJD (16 ビット) + BCD の時分秒 (24 ビット)。すべて 1 なら未定
fn decode_time(data: &[u8]) -> Result<Option<Time>, OutOfRange> {
    if data.len() >= 5 && data[..5] == [0xFF; 5] {
        return Ok(None);
    }
    let mjd = ((at(data, 0)? as u16) << 8) | at(data, 1)? as u16;
    Ok(Some((mjd, bcd(at(data, 2)?), bcd(at(data, 3)?), bcd(at(data, 4)?))))
}

fn decode_duration(data: &[u8]) -> Result<Option<Duration>, OutOfRange> {
    if data.len() >= 3 && data[..3] == [0xFF; 3] {
        return Ok(None);
    }
    Ok(Some((bcd(at(data, 0)?), bcd(at(data, 1)?), bcd(at(data, 2)?))))
}

/// 記述子 (タグ, 本体)。end が範囲の外を指すと OutOfRange
fn descriptors(data: &[u8], start: usize, end: usize) -> Result<Vec<(u8, &[u8])>, OutOfRange> {
    let mut out = Vec::new();
    let mut pos = start;
    while pos + 2 <= end {
        let (tag, length) = (at(data, pos)?, at(data, pos + 1)? as usize);
        let body = slice(data, pos + 2, pos + 2 + length);
        if pos + 2 + length > end {
            break;
        }
        out.push((tag, body));
        pos += 2 + length;
    }
    Ok(out)
}

fn text(data: &[u8]) -> String {
    if data.is_empty() { String::new() } else { arib::decode(data, false, false) }
}

fn u16_at(data: &[u8], pos: usize) -> Result<u16, OutOfRange> {
    Ok(((at(data, pos)? as u16) << 8) | at(data, pos + 1)? as u16)
}

// ---- セクションの解析 -----------------------------------------------------

/// SDT の (transport_stream_id, original_network_id, サービスの一覧 (service_id, 事業者名, 局名))
type Sdt = (u16, u16, Vec<(u16, String, String)>);

fn parse_sdt(section: &[u8]) -> Result<Sdt, OutOfRange> {
    let ts_id = u16_at(section, 3)?;
    let onid = u16_at(section, 8)?;
    let mut pos = 11;
    let end = section.len() as isize - 4;
    let mut services = Vec::new();
    while (pos + 5) as isize <= end {
        let service_id = u16_at(section, pos)?;
        let loop_len = length12(section, pos + 3)?;
        let (mut provider, mut name) = (String::new(), String::new());
        for (tag, body) in descriptors(section, pos + 5, pos + 5 + loop_len)? {
            if tag == 0x48 && body.len() >= 2 {
                // service_descriptor
                let prov_len = body[1] as usize;
                provider = text(slice(body, 2, 2 + prov_len));
                let name_len = if body.len() > 2 + prov_len { body[2 + prov_len] as usize } else { 0 };
                name = text(slice(body, 3 + prov_len, 3 + prov_len + name_len));
            }
        }
        services.push((service_id, provider, name));
        pos += 5 + loop_len;
    }
    Ok((ts_id, onid, services))
}

/// 拡張形式イベント記述子の項目を「項目名\n本文\n\n項目名\n…」にする。本文は記述子をまたいで
/// 続くことがあるので、次の項目名が来るまでバイト列のままつないでから文字に戻す
fn extended_text(descriptors: &[&[u8]]) -> String {
    let mut parts = String::new();
    let mut pending: Vec<u8> = Vec::new();
    let mut started = false;
    for body in descriptors {
        if body.len() < 5 {
            continue;
        }
        let loop_len = body[4] as usize;
        let mut pos = 5;
        let end = (5 + loop_len).min(body.len());
        while pos < end {
            let desc_len = body[pos] as usize;
            let heading = slice(body, pos + 1, pos + 1 + desc_len);
            let item_len = if pos + 1 + desc_len < body.len() { body[pos + 1 + desc_len] as usize } else { 0 };
            let item = slice(body, pos + 2 + desc_len, pos + 2 + desc_len + item_len);
            if desc_len > 0 {
                if started {
                    if !pending.is_empty() {
                        parts.push_str(&text(&pending));
                        pending.clear();
                    }
                    parts.push_str("\n\n");
                }
                parts.push_str(&text(heading));
                parts.push('\n');
                started = true;
            }
            pending.extend_from_slice(item);
            pos += 2 + desc_len + item_len;
        }
    }
    if !pending.is_empty() {
        parts.push_str(&text(&pending));
    }
    parts
}

/// ISO 639 の 3 文字。ASCII でないバイトは置換文字にし、前後の NUL と空白を除いて小文字にする
fn language(code: &[u8]) -> String {
    let s: String = code.iter().map(|&b| if b < 0x80 { b as char } else { '\u{FFFD}' }).collect();
    s.trim_matches(|c| c == '\0' || c == ' ').to_lowercase()
}

/// 音声コンポーネント記述子 (ARIB STD-B10) の本体 (9 バイト以上)
fn parse_audio_component(body: &[u8]) -> AudioComponent {
    let multi = body[5] & 0x80 != 0;
    let pos = if multi && body.len() >= 12 { 12 } else { 9 };
    let language2 = if multi && body.len() >= 12 { language(slice(body, 9, 12)) } else { String::new() };
    (body[2], body[1], body[3], body[5] & 0x40 != 0, language(slice(body, 6, 9)), language2, text(slice(body, pos, body.len())))
}

fn parse_eit(section: &[u8]) -> Result<Eit, OutOfRange> {
    let mut eit = Eit {
        service_id: u16_at(section, 3)?,
        transport_stream_id: u16_at(section, 8)?,
        original_network_id: u16_at(section, 10)?,
        section_number: at(section, 6)?,
        events: Vec::new(),
    };
    at(section, 5)?;
    let mut pos = 14;
    let end = section.len() as isize - 4;
    while (pos + 12) as isize <= end {
        let mut event = Event {
            event_id: u16_at(section, pos)?,
            start: decode_time(slice(section, pos + 2, pos + 7))?,
            duration: decode_duration(slice(section, pos + 7, pos + 10))?,
            name: String::new(),
            description: String::new(),
            extended: String::new(),
            genres: Vec::new(),
            audio_components: Vec::new(),
        };
        let loop_len = length12(section, pos + 10)?;
        let mut extended: Vec<&[u8]> = Vec::new();
        for (tag, body) in descriptors(section, pos + 12, pos + 12 + loop_len)? {
            match tag {
                0x4D if body.len() >= 4 => {
                    // short_event_descriptor
                    let name_len = body[3] as usize;
                    event.name = text(slice(body, 4, 4 + name_len));
                    let text_len = if body.len() > 4 + name_len { body[4 + name_len] as usize } else { 0 };
                    event.description = text(slice(body, 5 + name_len, 5 + name_len + text_len));
                }
                0x4E => extended.push(body), // extended_event_descriptor
                0x54 => {
                    // content_descriptor。0xE は拡張 (番組付属情報)
                    let mut n = 0;
                    while n + 1 < body.len() {
                        if body[n] & 0xF0 != 0xE0 {
                            event.genres.push(body[n]);
                        }
                        n += 2;
                    }
                }
                0xC4 if body.len() >= 9 => event.audio_components.push(parse_audio_component(body)),
                _ => {}
            }
        }
        // descriptor_number の順 (同じ番号は出た順)。空の記述子があれば、このセクションを捨てる
        if extended.iter().any(|b| b.is_empty()) {
            return Err(OutOfRange);
        }
        extended.sort_by_key(|b| b[0] >> 4);
        event.extended = extended_text(&extended);
        eit.events.push(event);
        pos += 12 + loop_len;
    }
    Ok(eit)
}

// ---- TS の読み込み ----------------------------------------------------------

/// PCR の入ったパケットの (行番号, PCR (27MHz))
fn pcr_values(packets: &[&[u8]], pid: u16) -> Vec<(usize, i64)> {
    let mut out = Vec::new();
    for (row, p) in packets.iter().enumerate() {
        let packet_pid = (((p[1] & 0x1F) as u16) << 8) | p[2] as u16;
        if packet_pid == pid && p[3] & 0x20 != 0 && p[4] >= 7 && p[5] & 0x10 != 0 && p[1] & 0x80 == 0 {
            let base = ((p[6] as i64) << 25) | ((p[7] as i64) << 17) | ((p[8] as i64) << 9) | ((p[9] as i64) << 1)
                | ((p[10] as i64) >> 7);
            let ext = (((p[10] & 0x01) as i64) << 8) | p[11] as i64;
            out.push((row, base * 300 + ext));
        }
    }
    out
}

fn pcr_seconds(pcr: i64, origin: i64) -> f64 {
    (pcr - origin).rem_euclid(PCR_WRAP) as f64 / PCR_CLOCK as f64
}

#[derive(Default)]
struct Reader {
    pat: Vec<(u16, u16)>,
    pmts: HashMap<u16, (u16, Vec<Stream>)>,
    sdt: HashMap<u16, SdtService>,
    eits: Vec<Eit>,
}

enum Collected {
    Times(Vec<(usize, Time)>),
    /// PAT を初めて読んだ。PMT の PID が分かったので、同じデータを読み直す
    Again,
}

impl Reader {
    /// PSI/SI を集め、時刻 (TDT/TOT) とその行番号を返す
    fn collect(&mut self, packets: &[&[u8]]) -> Vec<(usize, Time)> {
        loop {
            if let Collected::Times(times) = self.collect_once(packets) {
                return times;
            }
        }
    }

    fn collect_once(&mut self, packets: &[&[u8]]) -> Collected {
        let mut pids = vec![PID_PAT, PID_SDT, PID_EIT, PID_TOT];
        pids.extend(self.pat.iter().map(|p| p.1));
        let mut times = Vec::new();
        let eits_before = self.eits.len();
        let mut assemblers: HashMap<u16, SectionAssembler> = HashMap::new();
        for (row, packet) in packets.iter().enumerate() {
            let pid = (((packet[1] & 0x1F) as u16) << 8) | packet[2] as u16;
            if !pids.contains(&pid) {
                continue;
            }
            for section in assemblers.entry(pid).or_default().feed(packet) {
                let table = section[0];
                if table == TABLE_PAT && pid == PID_PAT {
                    if self.pat.is_empty() {
                        self.pat = parse_pat(&section);
                        // 番組の無い PAT では読み直さない (元の Python は読み直しを繰り返して止まらなかった)
                        if !self.pat.is_empty() {
                            // PAT より前に読んだ EIT は読み直しでもう一度集めるので捨てる (二重に持っていた、2026-11-17)
                            self.eits.truncate(eits_before);
                            return Collected::Again;
                        }
                    }
                } else if table == TABLE_PMT && self.pat.iter().any(|p| p.1 == pid) {
                    if let Ok((sid, pcr_pid, streams)) = parse_pmt(&section) {
                        self.pmts.entry(sid).or_insert((pcr_pid, streams));
                    }
                } else if table == TABLE_SDT_ACTUAL && pid == PID_SDT {
                    if let Ok((ts_id, onid, services)) = parse_sdt(&section) {
                        for (service_id, provider_name, service_name) in services {
                            self.sdt.entry(service_id).or_insert(SdtService {
                                transport_stream_id: ts_id, original_network_id: onid, provider_name, service_name,
                            });
                        }
                    }
                } else if table == TABLE_EIT_PF_ACTUAL && pid == PID_EIT {
                    if let Ok(eit) = parse_eit(&section) {
                        self.eits.push(eit);
                    }
                } else if (table == TABLE_TDT || table == TABLE_TOT) && pid == PID_TOT
                    && let Ok(Some(t)) = decode_time(slice(&section, 3, 8)) {
                    times.push((row, t));
                }
            }
        }
        Collected::Times(times)
    }

    /// 録画したサービス。映像のある最初のサービス (無ければ PAT の最初、PAT も無ければ最初の EIT)
    fn service_id(&self) -> Option<u16> {
        for &(number, _) in &self.pat {
            if let Some((_, streams)) = self.pmts.get(&number)
                && streams.iter().any(|s| VIDEO_STREAM_TYPES.contains(&s.1)) {
                return Some(number);
            }
        }
        if let Some(&(number, _)) = self.pat.first() {
            return Some(number);
        }
        self.eits.first().map(|e| e.service_id)
    }
}

fn time_mark(packets: &[&[u8]], times: &[(usize, Time)], pcr_pid: Option<u16>, origin: Option<i64>,
             last: bool) -> Option<TimeMark> {
    let &(row, t) = if last { times.last()? } else { times.first()? };
    let (Some(pcr_pid), Some(origin)) = (pcr_pid, origin) else { return Some((t, None)) };
    let pcrs = pcr_values(packets, pcr_pid);
    let before = pcrs.iter().rev().find(|(r, _)| *r <= row);
    let found = before.or_else(|| pcrs.iter().find(|(r, _)| *r > row));
    Some((t, found.map(|&(_, pcr)| pcr_seconds(pcr, origin))))
}

fn read_at(f: &mut File, offset: u64, length: u64) -> std::io::Result<Vec<u8>> {
    f.seek(SeekFrom::Start(offset))?;
    let mut data = Vec::with_capacity(length as usize);
    f.take(length).read_to_end(&mut data)?;
    Ok(data)
}

/// TS の番組情報を読む。TS でない・サービスが分からない時は None
pub fn read_program_info(path: &Path) -> std::io::Result<Option<ProgramInfo>> {
    let mut f = File::open(path)?;
    let size = f.metadata()?.len();
    let Some((packet_size, _)) = detect_packet_size(&read_at(&mut f, 0, 192 * 16)?) else { return Ok(None) };
    let mut reader = Reader::default();

    // 先頭: PAT・PMT・SDT・EIT と最初の時刻が揃うまで読む
    // 足りなければ CHUNK_SIZE ずつ読み足す。読んだ所は残し、足した分だけ読む (以前は毎回先頭から読み直し、
    // EIT・TOT の無い TS で先頭と末尾をそれぞれ 480 MiB 読んでいた、2026-11-17)。区切りは TS のパケットと
    // 合わないので、つなげたデータを毎回初めから解析する (結果は一度に読んだ時と同じ)
    let mut head_len = 0;
    let mut head_data = Vec::new();
    let mut head_times = Vec::new();
    while head_len < size.min(MAX_EDGE_SIZE) {
        let new_len = size.min(head_len + CHUNK_SIZE);
        head_data.extend_from_slice(&read_at(&mut f, head_len, new_len - head_len)?);
        head_len = new_len;
        reader = Reader::default();
        head_times = reader.collect(&packet_array(&head_data, packet_size));
        let sid = reader.service_id();
        if !head_times.is_empty()
            && let Some(sid) = sid
            && reader.pmts.contains_key(&sid)
            && reader.sdt.contains_key(&sid)
            && reader.eits.iter().any(|e| e.service_id == sid) {
            break;
        }
    }
    let head = packet_array(&head_data, packet_size);
    let Some(sid) = reader.service_id() else { return Ok(None) };
    let (pcr_pid, streams) = match reader.pmts.get(&sid) {
        Some((pcr, streams)) => (Some(*pcr), streams.clone()),
        None => (None, Vec::new()),
    };
    let origin = pcr_pid.and_then(|pid| pcr_values(&head, pid).first().map(|&(_, pcr)| pcr));
    let first = time_mark(&head, &head_times, pcr_pid, origin, false);
    let head_eits = std::mem::take(&mut reader.eits);

    // 中ほど (元の rplstool-cs が読む位置) と末尾
    let mut middle_eits = Vec::new();
    let mut tail_eits = Vec::new();
    let mut programme_streams = None;
    let mut last = None;
    let mut end_offset = None;
    if size > head_len {
        let head_pmts = std::mem::take(&mut reader.pmts);
        let mid_start = head_len.max((size / 2).saturating_sub(MIDDLE_SIZE / 2));
        let middle_data = read_at(&mut f, mid_start, MIDDLE_SIZE.min(size - mid_start))?;
        reader.collect(&packet_array(&middle_data, packet_size));
        middle_eits = std::mem::take(&mut reader.eits);
        programme_streams = Some(reader.pmts.get(&sid).map(|p| p.1.clone()).unwrap_or_default());
        reader.pmts = head_pmts;

        let mut tail_len = 0;
        let mut tail_data = Vec::new();
        let mut tail_times = Vec::new();
        while tail_len < (size - head_len).min(MAX_EDGE_SIZE) {
            // 末尾は後ろから読み足す: 足した分を前につなげる
            let new_len = (size - head_len).min(tail_len + CHUNK_SIZE);
            let mut data = read_at(&mut f, size - new_len, new_len - tail_len)?;
            data.extend_from_slice(&tail_data);
            tail_data = data;
            tail_len = new_len;
            reader.eits = Vec::new();
            tail_times = reader.collect(&packet_array(&tail_data, packet_size));
            if !tail_times.is_empty() {
                break;
            }
        }
        let tail = packet_array(&tail_data, packet_size);
        tail_eits = std::mem::take(&mut reader.eits);
        last = time_mark(&tail, &tail_times, pcr_pid, origin, true);
        if let (Some(pid), Some(origin)) = (pcr_pid, origin) {
            end_offset = pcr_values(&tail, pid).last().map(|&(_, pcr)| pcr_seconds(pcr, origin));
        }
    } else {
        if head_times.len() > 1 {
            last = time_mark(&head, &head_times, pcr_pid, origin, true);
        }
        if let (Some(pid), Some(origin)) = (pcr_pid, origin) {
            end_offset = pcr_values(&head, pid).last().map(|&(_, pcr)| pcr_seconds(pcr, origin));
        }
    }

    Ok(Some(ProgramInfo {
        packet_size, service_id: sid, pcr_pid, streams, programme_streams, sdt: reader.sdt.get(&sid).cloned(),
        first, last, end_offset, head_eits, middle_eits, tail_eits,
    }))
}

// ---- Python から呼ぶ ----------------------------------------------------------

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

fn eits_to_py<'py>(py: Python<'py>, eits: &[Eit]) -> PyResult<Bound<'py, PyList>> {
    let list = PyList::empty(py);
    for eit in eits {
        let events = PyList::empty(py);
        for e in &eit.events {
            let d = PyDict::new(py);
            d.set_item("event_id", e.event_id)?;
            d.set_item("start", e.start)?;
            d.set_item("duration", e.duration)?;
            d.set_item("name", &e.name)?;
            d.set_item("description", &e.description)?;
            d.set_item("extended", &e.extended)?;
            d.set_item("genres", &e.genres)?;
            d.set_item("audio_components", e.audio_components.clone())?;
            events.append(d)?;
        }
        let d = PyDict::new(py);
        d.set_item("service_id", eit.service_id)?;
        d.set_item("transport_stream_id", eit.transport_stream_id)?;
        d.set_item("original_network_id", eit.original_network_id)?;
        d.set_item("section_number", eit.section_number)?;
        d.set_item("events", events)?;
        list.append(d)?;
    }
    Ok(list)
}

/// TS の番組情報 (読んだままの値。日時は (MJD, 時, 分, 秒)、長さは (時, 分, 秒))。TS でない・サービスが
/// 分からない時は None。読めない時は OSError
#[pyfunction]
pub fn read_program_info_raw<'py>(py: Python<'py>, path: std::path::PathBuf) -> PyResult<Option<Bound<'py, PyDict>>> {
    let Some(info) = py.detach(|| read_program_info(&path))? else { return Ok(None) };
    let d = PyDict::new(py);
    d.set_item("packet_size", info.packet_size)?;
    d.set_item("service_id", info.service_id)?;
    d.set_item("pcr_pid", info.pcr_pid)?;
    d.set_item("streams", info.streams)?;
    d.set_item("programme_streams", info.programme_streams)?;
    d.set_item("sdt", info.sdt.map(|s| (s.transport_stream_id, s.original_network_id, s.service_name, s.provider_name)))?;
    d.set_item("first", info.first)?;
    d.set_item("last", info.last)?;
    d.set_item("end_offset", info.end_offset)?;
    d.set_item("head_eits", eits_to_py(py, &info.head_eits)?)?;
    d.set_item("middle_eits", eits_to_py(py, &info.middle_eits)?)?;
    d.set_item("tail_eits", eits_to_py(py, &info.tail_eits)?)?;
    Ok(Some(d))
}

/// ARIB STD-B24 の 8 単位符号を文字列に戻す (arib::decode)
#[pyfunction]
#[pyo3(signature = (data, apply_char_size=false, use_ivs=false))]
pub fn decode_arib_text(data: &[u8], apply_char_size: bool, use_ivs: bool) -> String {
    arib::decode(data, apply_char_size, use_ivs)
}
