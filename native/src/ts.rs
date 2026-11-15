//! TS のパケットと PSI/SI のセクションの読み方 (特徴の計算の scanner.rs と、番組情報の program_info.rs が使う)。
//! 元は rplsinfo の TS の読み方 (作者の記載により改変・再配布は自由)。元との違い:
//! - 188 / 192 バイトのパケットを、読んだデータから判定する (元は呼び出し側が指定)。
//! - 連続性カウンタの飛び (パケットの欠落) を見て、途中のセクションを捨てる。
//! - 1 つのパケットの中の、2 つ目以降のセクションも取り出す。

pub const TS_PACKET_SIZE: usize = 188;
pub const SYNC_BYTE: u8 = 0x47;
pub const PACKET_SIZES: [usize; 2] = [188, 192];
const SYNC_CHECK_PACKETS: usize = 8;
/// 映像の stream_type (MPEG-2、H.264、H.265)
pub const VIDEO_STREAM_TYPES: [u8; 4] = [0x01, 0x02, 0x1B, 0x24];

/// 範囲の外を読んだ (解析の失敗として扱う)
#[derive(Debug)]
pub struct OutOfRange;

pub fn at(data: &[u8], pos: usize) -> Result<u8, OutOfRange> {
    data.get(pos).copied().ok_or(OutOfRange)
}

pub fn length12(data: &[u8], pos: usize) -> Result<usize, OutOfRange> {
    Ok((((at(data, pos)? & 0x0F) as usize) << 8) | at(data, pos + 1)? as usize)
}

/// start 以降で、パケットが続けて並ぶ最初の位置。limit を渡すと start から limit バイト以内だけを探す
pub fn find_sync(data: &[u8], packet_size: usize, start: usize, limit: Option<usize>) -> Option<usize> {
    let offset = packet_size - TS_PACKET_SIZE;
    let mut last = data.len() as isize - packet_size as isize;
    if let Some(limit) = limit {
        last = last.min((start + limit) as isize);
    }
    let mut pos = start as isize;
    while pos <= last {
        let from = pos as usize + offset;
        let to = last as usize + offset + 1;
        let found = from + data[from..to].iter().position(|&b| b == SYNC_BYTE)?;
        pos = (found - offset) as isize;
        let p = pos as usize;
        let checks = SYNC_CHECK_PACKETS.min((data.len() - p) / packet_size);
        if (1..checks).all(|k| data[p + offset + k * packet_size] == SYNC_BYTE) {
            return Some(p);
        }
        pos += 1;
    }
    None
}

/// (パケットの大きさ, 最初のパケットの位置)。TS でなければ None
pub fn detect_packet_size(data: &[u8]) -> Option<(usize, usize)> {
    let mut best: Option<(usize, usize)> = None;
    for size in PACKET_SIZES {
        if let Some(start) = find_sync(data, size, 0, Some(size * 4))
            && best.is_none_or(|(_, s)| start < s) {
                best = Some((size, start));
            }
    }
    best
}

/// データを 188 バイトのパケットに分ける。同期がずれた所は取り直す
pub fn packet_array(data: &[u8], packet_size: usize) -> Vec<&[u8]> {
    let offset = packet_size - TS_PACKET_SIZE;
    let mut packets = Vec::new();
    let mut pos = 0;
    while let Some(found) = find_sync(data, packet_size, pos, None) {
        pos = found;
        let count = (data.len() - pos) / packet_size;
        if count == 0 {
            break;
        }
        let row = |k: usize| &data[pos + k * packet_size + offset..pos + (k + 1) * packet_size];
        match (0..count).position(|k| data[pos + k * packet_size + offset] != SYNC_BYTE) {
            None => {
                packets.extend((0..count).map(row));
                break;
            }
            Some(bad) => {
                packets.extend((0..bad).map(row));
                pos += bad * packet_size + 1;
            }
        }
    }
    packets
}

/// MPEG-2 の CRC32 (多項式 0x04C11DB7、初期値 0xFFFFFFFF)。末尾の CRC まで含めると、壊れていなければ 0
pub fn crc32_mpeg2(data: &[u8]) -> u32 {
    static TABLE: std::sync::OnceLock<[u32; 256]> = std::sync::OnceLock::new();
    let table = TABLE.get_or_init(|| {
        let mut t = [0u32; 256];
        for (i, v) in t.iter_mut().enumerate() {
            let mut c = (i as u32) << 24;
            for _ in 0..8 {
                c = if c & 0x8000_0000 != 0 { (c << 1) ^ 0x04C1_1DB7 } else { c << 1 };
            }
            *v = c;
        }
        t
    });
    data.iter().fold(0xFFFF_FFFF, |crc, &b| (crc << 8) ^ table[((crc >> 24) as u8 ^ b) as usize])
}

/// CRC の付いたセクションか。TDT (0x70) だけは CRC が無い
pub fn has_crc(section: &[u8]) -> bool {
    section[1] & 0x80 != 0 || section[0] == 0x73
}

/// 1 つの PID のパケットを順に受け取り、完成したセクションを返す
#[derive(Default)]
pub struct SectionAssembler {
    buf: Vec<u8>,
    active: bool,
    cc: Option<u8>,
}

impl SectionAssembler {
    pub fn feed(&mut self, packet: &[u8]) -> Vec<Vec<u8>> {
        if packet[1] & 0x80 != 0 {
            self.reset();
            return Vec::new();
        }
        let afc = (packet[3] >> 4) & 0x03;
        if afc & 0x01 == 0 {
            return Vec::new();
        }
        let cc = packet[3] & 0x0F;
        if let Some(previous) = self.cc {
            if cc == previous {
                return Vec::new(); // 同じパケットの再送
            }
            if cc != (previous + 1) & 0x0F {
                self.reset(); // パケットが欠けた
            }
        }
        self.cc = Some(cc);

        let mut pos = 4;
        if afc == 0x03 {
            pos += 1 + packet[4] as usize;
        }
        if pos >= TS_PACKET_SIZE {
            return Vec::new();
        }
        let payload = &packet[pos..];
        let mut out = Vec::new();
        if packet[1] & 0x40 != 0 {
            // payload_unit_start_indicator: 先頭は pointer_field
            let pointer = payload[0] as usize;
            if self.active {
                self.buf.extend_from_slice(&payload[1.min(payload.len())..(1 + pointer).min(payload.len())]);
                self.drain(&mut out);
            }
            self.buf = payload[(1 + pointer).min(payload.len())..].to_vec();
            self.active = true;
        } else if self.active {
            self.buf.extend_from_slice(payload);
        } else {
            return out;
        }
        self.drain(&mut out);
        out
    }

    fn reset(&mut self) {
        self.buf.clear();
        self.active = false;
    }

    fn drain(&mut self, out: &mut Vec<Vec<u8>>) {
        while self.active && self.buf.len() >= 3 {
            if self.buf[0] == 0xFF {
                // 詰め物。このパケットの残りにセクションは無い
                self.reset();
                return;
            }
            let length = ((((self.buf[1] & 0x0F) as usize) << 8) | self.buf[2] as usize) + 3;
            if self.buf.len() < length {
                return;
            }
            let section: Vec<u8> = self.buf.drain(..length).collect();
            if !has_crc(&section) || crc32_mpeg2(&section) == 0 {
                out.push(section);
            }
        }
    }
}

/// program_number → PMT の PID (NIT を指す 0 番は除く)。並びは最初に出た順、同じ番号が続けば後のもの
pub fn parse_pat(section: &[u8]) -> Vec<(u16, u16)> {
    let mut programs: Vec<(u16, u16)> = Vec::new();
    let end = section.len() as isize - 4;
    let mut pos = 8isize;
    while pos < end - 3 {
        let p = pos as usize;
        let number = ((section[p] as u16) << 8) | section[p + 1] as u16;
        let pid = (((section[p + 2] & 0x1F) as u16) << 8) | section[p + 3] as u16;
        if number != 0 {
            match programs.iter_mut().find(|x| x.0 == number) {
                Some(entry) => entry.1 = pid,
                None => programs.push((number, pid)),
            }
        }
        pos += 4;
    }
    programs
}

pub type Stream = (u16, u8, Option<u8>);

/// (service_id, PCR の PID, ES の一覧 (pid, stream_type, component_tag))
pub fn parse_pmt(section: &[u8]) -> Result<(u16, u16, Vec<Stream>), OutOfRange> {
    let service_id = ((at(section, 3)? as u16) << 8) | at(section, 4)? as u16;
    let pcr_pid = (((at(section, 8)? & 0x1F) as u16) << 8) | at(section, 9)? as u16;
    let mut pos = 12 + length12(section, 10)?;
    let end = section.len().saturating_sub(4);
    let mut streams = Vec::new();
    while pos + 5 <= end {
        let stream_type = section[pos];
        let pid = (((section[pos + 1] & 0x1F) as u16) << 8) | section[pos + 2] as u16;
        let info_len = length12(section, pos + 3)?;
        let mut tag = None;
        // 記述子
        let (mut d, d_end) = (pos + 5, pos + 5 + info_len);
        while d + 2 <= d_end {
            let (d_tag, length) = (at(section, d)?, at(section, d + 1)? as usize);
            if d + 2 + length > d_end {
                break;
            }
            let body = &section[(d + 2).min(section.len())..(d + 2 + length).min(section.len())];
            if d_tag == 0x52 && !body.is_empty() {
                tag = Some(body[0]); // stream_identifier_descriptor
            }
            d += 2 + length;
        }
        streams.push((pid, stream_type, tag));
        pos += 5 + info_len;
    }
    Ok((service_id, pcr_pid, streams))
}
