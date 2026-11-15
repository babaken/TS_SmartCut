//! 音声の形式・字幕の変化 (結果の形と説明は ts_smartcut/autocut/stream_events.py)。
//! パケットの大きさの判定・同期の取り直し・セクションの組み立て・PAT/PMT の読み方は ts.rs (番組情報と共通)。

use std::collections::{HashMap, HashSet};

use crate::ts::{OutOfRange, SectionAssembler, Stream, VIDEO_STREAM_TYPES, detect_packet_size, packet_array, parse_pat, parse_pmt};

// パケットの大きさを判定するのに見る量 (stream_events.CHUNK_SIZE)
const DETECT_SIZE: usize = 32 * 1024 * 1024;
const PTS_WRAP: i64 = 1 << 33;
const AUDIO_STREAM_TYPES: [u8; 5] = [0x0F, 0x11, 0x03, 0x04, 0x81];
const ADTS_STREAM_TYPE: u8 = 0x0F;

/// PES の始まりのパケットのペイロード (アダプテーションフィールドを除く)
fn pes_payload(packet: &[u8]) -> Option<&[u8]> {
    let afc = (packet[3] >> 4) & 0x03;
    if afc & 0x01 == 0 {
        return None;
    }
    let mut pos = 4;
    if afc == 0x03 {
        pos += 1 + packet[4] as usize;
    }
    if pos >= packet.len() {
        return None;
    }
    Some(&packet[pos..])
}

/// (PTS, PES のデータの始まりの位置)。PES でなければ None
fn pes_pts(payload: &[u8]) -> Option<(Option<i64>, usize)> {
    if payload.len() < 9 || payload[0] != 0 || payload[1] != 0 || payload[2] != 1 {
        return None;
    }
    let header_end = 9 + payload[8] as usize;
    let mut pts = None;
    if payload[7] & 0x80 != 0 && payload.len() >= 14 {
        let p = &payload[9..14];
        pts = Some(
            (((p[0] as i64 >> 1) & 0x07) << 30)
                | ((p[1] as i64) << 22)
                | ((p[2] as i64 >> 1) << 15)
                | ((p[3] as i64) << 7)
                | (p[4] as i64 >> 1),
        );
    }
    Some((pts, header_end))
}

/// PES のデータの先頭の ADTS ヘッダの (profile と sampling_frequency_index, channel_configuration)
fn adts_header(data: &[u8]) -> Option<(u8, u8)> {
    if data.len() < 7 || data[0] != 0xFF || (data[1] & 0xF6) != 0xF0 {
        return None;
    }
    if ((data[2] >> 2) & 0x0F) > 12 {
        return None;
    }
    let frame_length = (((data[3] & 0x03) as u32) << 11) | ((data[4] as u32) << 3) | ((data[5] as u32) >> 5);
    if frame_length < 7 {
        return None;
    }
    Some((data[2] >> 2, ((data[2] & 0x01) << 2) | (data[3] >> 6)))
}

/// 字幕の PES のデータが字幕文 (管理データでない) か。ARIB STD-B24 第三編
fn is_caption_statement(data: &[u8]) -> bool {
    if data.len() < 4 || data[0] != 0x80 {
        return false;
    }
    let pos = 3 + (data[2] & 0x0F) as usize;
    if pos >= data.len() {
        return false;
    }
    let group_id = data[pos] >> 2;
    group_id & 0x0F != 0 // 0x00 / 0x20 は管理データ
}

fn data_from(payload: &[u8], start: usize, len: Option<usize>) -> &[u8] {
    let s = start.min(payload.len());
    let e = len.map_or(payload.len(), |n| (start + n).min(payload.len()));
    &payload[s..e.max(s)]
}

/// 録画のデータを先頭から順に受け取り (feed_bytes)、変化を集める
pub struct Scanner {
    packet_size: Option<usize>,
    rest: Vec<u8>,
    service_id: Option<u16>,
    video_start_ticks: i64,
    assemblers: HashMap<u16, SectionAssembler>,
    pmt_pid: Option<u16>,
    streams: Option<Vec<Stream>>,
    video_pid: Option<u16>,
    audio_pid: Option<u16>,
    audio_is_adts: bool,
    caption_pids: HashSet<u16>,
    clock: Option<f64>,
    pub pmt_changes: Vec<(f64, Vec<Stream>)>,
    pub audio_channels: Vec<(f64, u8)>,
    pub captions: Vec<f64>,
    channels: Option<u8>,
    rate: Option<u8>,
    pending: Option<(u8, u8)>,
    pending_time: f64,
}

impl Scanner {
    pub fn new(service_id: Option<u16>, video_start: f64) -> Self {
        Scanner {
            packet_size: None,
            rest: Vec::new(),
            service_id,
            // 偶数への丸め (Python の round と同じ。記録した特徴と揃える)
            video_start_ticks: (video_start * 90000.0).round_ties_even() as i64,
            assemblers: HashMap::new(),
            pmt_pid: None,
            streams: None,
            video_pid: None,
            audio_pid: None,
            audio_is_adts: false,
            caption_pids: HashSet::new(),
            clock: None,
            pmt_changes: Vec::new(),
            audio_channels: Vec::new(),
            captions: Vec::new(),
            channels: None,
            rate: None,
            pending: None,
            pending_time: 0.0,
        }
    }

    fn seconds(&self, pts: i64) -> f64 {
        let mut d = (pts - self.video_start_ticks).rem_euclid(PTS_WRAP);
        if d >= PTS_WRAP / 2 {
            d -= PTS_WRAP;
        }
        d as f64 / 90000.0
    }

    fn es_pids(&self) -> (Option<u16>, Option<u16>, Vec<u16>) {
        let mut captions: Vec<u16> = self.caption_pids.iter().copied().collect();
        captions.sort_unstable();
        (self.video_pid, self.audio_pid, captions)
    }

    fn apply_pmt(&mut self, section: &[u8]) -> Result<(), OutOfRange> {
        let (service_id, _pcr_pid, layout) = parse_pmt(section)?;
        if self.service_id.is_some_and(|sid| sid != service_id) {
            return Ok(());
        }
        if self.streams.as_ref() == Some(&layout) {
            return Ok(());
        }
        self.pmt_changes.push((self.clock.unwrap_or(0.0), layout.clone()));
        self.video_pid = layout.iter().find(|s| VIDEO_STREAM_TYPES.contains(&s.1)).map(|s| s.0);
        let audio = layout.iter().find(|s| AUDIO_STREAM_TYPES.contains(&s.1));
        self.audio_pid = audio.map(|s| s.0);
        self.audio_is_adts = audio.is_some_and(|s| s.1 == ADTS_STREAM_TYPE);
        self.caption_pids =
            layout.iter().filter(|s| s.1 == 0x06 && s.2.is_some_and(|t| (0x30..0x38).contains(&t))).map(|s| s.0).collect();
        self.streams = Some(layout);
        Ok(())
    }

    /// チャンネル数の変化を記録する。サンプリング周波数とプロファイルが直前と同じで、
    /// 同じ値が 2 回続いた時だけ変化とみなす
    fn audio_header(&mut self, header: Option<(u8, u8)>, when: f64) {
        let Some((rate, channels)) = header else { return };
        let Some(current) = self.channels else {
            if self.pending == Some((rate, channels)) {
                self.channels = Some(channels);
                self.rate = Some(rate);
                self.audio_channels.push((self.pending_time, channels));
            } else {
                self.pending = Some((rate, channels));
                self.pending_time = when;
            }
            return;
        };
        if Some(rate) != self.rate || channels == current {
            self.pending = None;
            return;
        }
        if self.pending == Some((rate, channels)) {
            self.channels = Some(channels);
            self.audio_channels.push((self.pending_time, channels));
            self.pending = None;
        } else {
            self.pending = Some((rate, channels));
            self.pending_time = when;
        }
    }

    /// パケットを順に見る。1 パケットずつ、その時の PID で選ぶ
    fn feed(&mut self, packets: &[&[u8]]) -> Result<(), OutOfRange> {
        for packet in packets {
            if packet[1] & 0x80 != 0 {
                continue; // transport_error_indicator
            }
            let pid = (((packet[1] & 0x1F) as u16) << 8) | packet[2] as u16;
            if pid == 0 || Some(pid) == self.pmt_pid {
                let sections = self.assemblers.entry(pid).or_default().feed(packet);
                for section in sections {
                    if section[0] == 0x00 {
                        let programs = parse_pat(&section);
                        if self.service_id.is_none() && !programs.is_empty() {
                            self.service_id = programs.iter().map(|p| p.0).min();
                        }
                        let pmt = self.service_id.and_then(|sid| programs.iter().find(|p| p.0 == sid)).map(|p| p.1);
                        if pmt.is_some() && pmt != self.pmt_pid {
                            // 見る PID が変わった (このパケットの残りのセクションは見ない)
                            self.pmt_pid = pmt;
                            break;
                        }
                    } else if section[0] == 0x02 {
                        let before = self.es_pids();
                        self.apply_pmt(&section)?;
                        if self.es_pids() != before {
                            break;
                        }
                    }
                }
                continue;
            }
            if packet[1] & 0x40 == 0 {
                continue;
            }
            let is_es = Some(pid) == self.video_pid || Some(pid) == self.audio_pid || self.caption_pids.contains(&pid);
            if !is_es {
                continue;
            }
            let Some(payload) = pes_payload(packet) else { continue };
            let Some((pts, start)) = pes_pts(payload) else { continue };
            if Some(pid) == self.video_pid {
                if let Some(pts) = pts {
                    self.clock = Some(self.seconds(pts));
                }
                continue;
            }
            let Some(when) = pts.map(|p| self.seconds(p)).or(self.clock) else { continue };
            if Some(pid) == self.audio_pid && self.audio_is_adts {
                self.audio_header(adts_header(data_from(payload, start, Some(16))), when);
            } else if self.caption_pids.contains(&pid) && is_caption_statement(data_from(payload, start, None)) {
                self.captions.push(when);
            }
        }
        Ok(())
    }

    /// 録画の続きのデータを解析する (パケットの途中で切れていてよい)。解析に失敗した時は、
    /// このデータの残りを見ずに Err を返す (続きのデータは見る)
    pub fn feed_bytes(&mut self, data: &[u8]) -> Result<(), OutOfRange> {
        let mut buf = std::mem::take(&mut self.rest);
        buf.extend_from_slice(data);
        let mut start = 0;
        if self.packet_size.is_none() {
            match detect_packet_size(&buf[..buf.len().min(DETECT_SIZE)]) {
                None => {
                    // TS の始まりが見つかるまで待つ
                    self.rest = buf[buf.len().saturating_sub(192 * 8)..].to_vec();
                    return Ok(());
                }
                Some((size, s)) => {
                    self.packet_size = Some(size);
                    start = s;
                }
            }
        }
        let size = self.packet_size.unwrap();
        let usable = (buf.len() - start) / size * size;
        self.rest = buf[start + usable..].to_vec();
        if usable > 0 {
            let packets = packet_array(&buf[start..start + usable], size);
            self.feed(&packets)?;
        }
        Ok(())
    }
}
