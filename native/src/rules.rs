//! ルールによる案づくりの決まり (設計書 §31.3・§31.5、自動カット計画 A5〜A7)。2026-10-28 からここだけにある
//! (それまでは Python の candidates.py・rules.py・proposals.py・evaluate.py と、自動補正用の tuning.rs の 2 つ)。
//!
//! - 境目の候補: 無音と場面の切り替わりが重なる位置 (candidates.py の説明)。
//! - 区間の判定: 本編・CM・番組の前後と、確信度・理由の種類 (rules.py の説明。理由の文は Python が言語ファイルから作る)。
//! - 案: 本編の区間をつなぎ、次が切る区間なら局の値だけ手前で終える。
//! - 局の値: 過去のカットから測る (区間の終わりの差・本編と CM の間の短い区間を残したか)。
//! - 正解との比較 (evaluate.compare)。
//!
//! 数値は numpy で計算していた時と同じ: 音量の dB と場面の切り替わりは float32 で比べ、round は偶数への丸め。

use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::sync::Arc;

// thresholds.SPECS の順 (Python は THRESHOLD_NAMES と SPECS が同じかを確かめてから使う)
pub const THRESHOLD_NAMES: [&str; 15] = [
    "silence_db",
    "min_silence",
    "long_silence",
    "scene_min",
    "min_gap",
    "logo_program",
    "logo_cm",
    "length_tolerance",
    "max_cm_seconds",
    "long_tolerance",
    "long_cm_seconds",
    "short_seconds",
    "min_segment_seconds",
    "long_program_seconds",
    "silent_fraction",
];

// 無音の前後で場面の切り替わりを探す幅 (フレーム)
const PAD_FRAMES: i64 = 6;
// ロゴの無い区間は、15 秒の倍数とみなす差を広げる (CM の間の境目が 0.5 秒ほどずれ、15.5 秒と 14.5 秒が並ぶ局があった。BS-A)
const NO_LOGO_TOLERANCE: f64 = 1.0;
// 両側を切る区間に挟まれた長い番宣は、15 秒の倍数との差をここまで許す (後ろに 1 秒の無音が付いて 181 秒になった CS-A の番宣)
const SANDWICH_TOLERANCE: f64 = 1.1;
const EDGE_SKIP_SECONDS: f64 = 0.5; // ロゴの平均を取る時に除く区間の両端
pub const DEFAULT_END_OFFSET: i64 = 1; // 残す区間の終わりを境目の何フレーム手前にするか (局の記録が無い時)
// 残す区間の終わりと境目の差を測る時に、同じ境目とみなす最大のフレーム数
const END_MATCH_FRAMES: i64 = 3;
// 「本編と CM の間の短い区間」を残すか切るかを局の癖とするのに必要な例の数
const MIN_SHORT_BETWEEN_SAMPLES: usize = 3;
// 音量の保存形式 (features.LEVEL_FLOOR_DB): dB = q/2 - 100
const LEVEL_FLOOR_DB: f32 = -100.0;

#[derive(Clone, Copy)]
pub struct Thresholds {
    pub silence_db: f64,
    pub min_silence: f64,
    pub long_silence: f64,
    pub scene_min: f64,
    pub min_gap: f64,
    pub logo_program: f64,
    pub logo_cm: f64,
    pub length_tolerance: f64,
    pub max_cm_seconds: f64,
    pub long_tolerance: f64,
    pub long_cm_seconds: f64,
    pub short_seconds: f64,
    pub min_segment_seconds: f64,
    pub long_program_seconds: f64,
    pub silent_fraction: f64,
}

impl Thresholds {
    pub fn from_values(v: &[f64]) -> PyResult<Thresholds> {
        if v.len() != THRESHOLD_NAMES.len() {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "expected {} thresholds, got {}",
                THRESHOLD_NAMES.len(),
                v.len()
            )));
        }
        Ok(Thresholds {
            silence_db: v[0],
            min_silence: v[1],
            long_silence: v[2],
            scene_min: v[3],
            min_gap: v[4],
            logo_program: v[5],
            logo_cm: v[6],
            length_tolerance: v[7],
            max_cm_seconds: v[8],
            long_tolerance: v[9],
            long_cm_seconds: v[10],
            short_seconds: v[11],
            min_segment_seconds: v[12],
            long_program_seconds: v[13],
            silent_fraction: v[14],
        })
    }

    /// 境目の候補を覚えておく鍵 (候補に効く値)
    pub fn candidate_key(&self) -> [u64; 5] {
        [
            self.silence_db.to_bits(),
            self.min_silence.to_bits(),
            self.long_silence.to_bits(),
            self.scene_min.to_bits(),
            self.min_gap.to_bits(),
        ]
    }
}

#[derive(Clone, Copy)]
pub struct Boundary {
    pub frame: i64,    // 新しい区間の最初のフレーム
    pub silence: f64,  // 無音の長さ (秒)
    pub scene: f64,    // 場面の切り替わりの強さ (0 = 見つからない)
}

#[derive(Clone, Copy)]
pub struct Segment {
    pub start: i64, // [start, end)
    pub end: i64,
    pub fps: f64,
}

impl Segment {
    pub fn seconds(&self) -> f64 {
        (self.end - self.start) as f64 / self.fps
    }
}

#[derive(Clone, Copy, PartialEq, Debug)]
pub enum Kind {
    Program,
    Cm,
    Margin,
}

impl Kind {
    pub fn name(self) -> &'static str {
        match self {
            Kind::Program => "program",
            Kind::Cm => "cm",
            Kind::Margin => "margin",
        }
    }

    pub fn from_name(name: &str) -> PyResult<Kind> {
        match name {
            "program" => Ok(Kind::Program),
            "cm" => Ok(Kind::Cm),
            "margin" => Ok(Kind::Margin),
            _ => Err(pyo3::exceptions::PyValueError::new_err(format!("unknown kind {name:?}"))),
        }
    }
}

/// 判定の理由の種類 (Python は言語ファイルの reasons.<名前> の文にする)
#[derive(Clone, Copy, PartialEq, Debug)]
pub enum Reason {
    MarginBefore,      // 番組の前 (放送時間の外)
    MarginAfter,       // 番組の後
    LogoYes,           // ロゴあり
    LogoNo,            // ロゴなしで 15 秒の倍数
    Long,              // CM にしては長い
    CmMultiples,       // 15 秒の倍数が 3 つ以上続く
    MarginShort,       // 番組の前後と本編の間の短い区間
    SilentNextToCm,    // ほぼ無音で CM の隣
    CmContinued,       // CM の続き (15 秒の倍数・番宣)
    CmLongPromo,       // 長い番宣
    CmSandwichedPromo, // 切る区間に挟まれた長い番宣
    ShortBetweenCut,   // 本編と CM の間の短い区間 (局の記録で切る)
    ShortBetweenKept,  // 本編と CM の間の短い区間 (残す)
    NotCm,             // CM の決め手が無い
}

impl Reason {
    pub fn name(self) -> &'static str {
        match self {
            Reason::MarginBefore => "margin_before",
            Reason::MarginAfter => "margin_after",
            Reason::LogoYes => "logo_yes",
            Reason::LogoNo => "logo_no",
            Reason::Long => "long",
            Reason::CmMultiples => "cm_multiples",
            Reason::MarginShort => "margin_short",
            Reason::SilentNextToCm => "silent_next_to_cm",
            Reason::CmContinued => "cm_continued",
            Reason::CmLongPromo => "cm_long_promo",
            Reason::CmSandwichedPromo => "cm_sandwiched_promo",
            Reason::ShortBetweenCut => "short_between_cut",
            Reason::ShortBetweenKept => "short_between_kept",
            Reason::NotCm => "not_cm",
        }
    }
}

#[derive(Clone, Copy)]
pub struct Decision {
    pub segment: Segment,
    pub kind: Kind,
    pub confidence: f64,
    pub reason: Reason,
    pub logo: Option<f64>,   // ロゴの一致度の平均 (ロゴの無い局は None)
    pub short_between: bool, // 本編と CM の間の短い区間 (rules.SHORT_BETWEEN)
}

#[derive(Clone, Copy)]
pub struct Profile {
    pub end_offset: i64,
    pub short_between_kept: Option<f64>,
}

impl Default for Profile {
    fn default() -> Self {
        Profile { end_offset: DEFAULT_END_OFFSET, short_between_kept: None }
    }
}

/// float16 のビットを float32 にする (numpy の astype(float32) と同じ。どの値も正確に表せる)
fn f16_to_f32(bits: u16) -> f32 {
    let sign = ((bits >> 15) as u32) << 31;
    let exponent = ((bits >> 10) & 0x1F) as u32;
    let mantissa = (bits & 0x3FF) as u32;
    let value = if exponent == 0 {
        if mantissa == 0 {
            sign
        } else {
            // 非正規化数: 正規化し直す
            let mut e: i32 = 1;
            let mut m = mantissa;
            while m & 0x400 == 0 {
                m <<= 1;
                e -= 1;
            }
            sign | (((e + 127 - 15) as u32) << 23) | ((m & 0x3FF) << 13)
        }
    } else if exponent == 0x1F {
        sign | 0x7F80_0000 | (mantissa << 13)
    } else {
        sign | ((exponent + 127 - 15) << 23) | (mantissa << 13)
    };
    f32::from_bits(value)
}

/// 録画 1 本の特徴のうち、案づくりに使うもの (features.RecordingFeatures から作る)。
/// 音量は保存形式のまま (0.5dB 刻みの uint8)、場面の切り替わりは float16 のまま持ち、使う時に float32 にする。
pub struct Recording {
    pub fps: f64,
    pub frame_count: i64,
    audio_rate: f64,
    audio_offset: f64,
    audio_level: Vec<u8>,
    scene: Vec<u16>,
    // ロゴの一致度の累積和 (ロゴのある隅ごとに frame + 1 個)。ロゴの無い局は空
    logo_cumsum: Vec<Vec<i32>>,
}

/// candidates.nearest_multiple_error: 長さと、いちばん近い unit 秒の倍数 (unit 以上) との差 (秒)
pub fn nearest_multiple_error(seconds: f64, unit: f64) -> f64 {
    let k = (seconds / unit).round_ties_even().max(1.0);
    (seconds - k * unit).abs()
}

/// CM 1 本らしい長さ (5 秒の倍数、max_cm_seconds まで)
fn is_cm_length(seconds: f64, t: &Thresholds) -> bool {
    seconds <= t.max_cm_seconds + t.length_tolerance && nearest_multiple_error(seconds, 5.0) <= t.length_tolerance
}

/// 長い番宣らしい長さ (15 秒の倍数にぴったり、long_cm_seconds まで)。CM が続く所だけで使う
fn is_long_cm_length(seconds: f64, t: &Thresholds) -> bool {
    seconds <= t.long_cm_seconds && nearest_multiple_error(seconds, 15.0) <= t.long_tolerance
}

/// CM 1 本らしい長さのうち、15 秒の倍数 (15・30・60 秒の CM)
fn is_strict_cm_length(seconds: f64, t: &Thresholds, tolerance: f64) -> bool {
    seconds <= t.max_cm_seconds + tolerance && nearest_multiple_error(seconds, 15.0) <= tolerance
}

impl Recording {
    fn audio_db(&self, i: usize) -> f32 {
        // features.audio_db(): uint8 を float32 にして / 2 + LEVEL_FLOOR_DB
        self.audio_level[i] as f32 / 2.0 + LEVEL_FLOOR_DB
    }

    /// 無音の (始まり, 終わり) の時刻 (秒、映像の先頭が 0)
    pub fn find_silences(&self, threshold_db: f64, min_seconds: f64) -> Vec<(f64, f64)> {
        let threshold = threshold_db as f32;
        let rate = self.audio_rate;
        let mut result = Vec::new();
        let mut start: Option<usize> = None;
        let n = self.audio_level.len();
        for i in 0..=n {
            let quiet = i < n && self.audio_db(i) < threshold;
            match (quiet, start) {
                (true, None) => start = Some(i),
                (false, Some(a)) => {
                    if (i - a) as f64 / rate >= min_seconds {
                        result.push((self.audio_offset + a as f64 / rate, self.audio_offset + i as f64 / rate));
                    }
                    start = None;
                }
                _ => {}
            }
        }
        result
    }

    /// 境目の候補 (フレームの順)。無音の前後 PAD_FRAMES を含む範囲で、場面の切り替わりが最も強いフレーム。
    /// long_silence 秒以上の無音では、scene_min 以上の山を強い順に min_gap 秒以上離して全部。見つからなければ無音の中ほど
    pub fn find_boundaries(&self, t: &Thresholds) -> Vec<Boundary> {
        let scene_min = t.scene_min as f32;
        let fps = self.fps;
        let n = self.frame_count;
        let mut result: Vec<Boundary> = Vec::new();
        for (t0, t1) in self.find_silences(t.silence_db, t.min_silence) {
            let a = ((t0 * fps).floor() as i64 - PAD_FRAMES).max(1);
            let b = ((t1 * fps).ceil() as i64 + PAD_FRAMES).min(n - 1);
            if b <= a {
                continue;
            }
            // numpy のスライスと同じく配列の長さで切る
            let len = self.scene.len() as i64;
            let window: Vec<f32> =
                self.scene[a.min(len) as usize..b.min(len) as usize].iter().map(|&h| f16_to_f32(h)).collect();
            let mut found: Vec<(i64, f32)> = Vec::new();
            if t1 - t0 >= t.long_silence {
                let mut peaks: Vec<usize> = (0..window.len())
                    .filter(|&k| {
                        let w = window[k];
                        let left = if k == 0 { -1.0 } else { window[k - 1] };
                        let right = if k + 1 == window.len() { -1.0 } else { window[k + 1] };
                        w >= scene_min && w > left && w >= right
                    })
                    .collect();
                // 強い順 (同じ強さは前から)
                peaks.sort_by(|&x, &y| window[y].partial_cmp(&window[x]).unwrap());
                let mut chosen: Vec<i64> = Vec::new();
                for k in peaks {
                    let k = k as i64;
                    if chosen.iter().all(|&c| ((k - c).abs() as f64) >= t.min_gap * fps) {
                        chosen.push(k);
                    }
                }
                chosen.sort();
                found = chosen.iter().map(|&k| (a + k, window[k as usize])).collect();
            } else if !window.is_empty() {
                let mut k = 0;
                for (i, &w) in window.iter().enumerate() {
                    if w > window[k] {
                        k = i;
                    }
                }
                if window[k] >= scene_min {
                    found.push((a + k as i64, window[k]));
                }
            }
            if found.is_empty() {
                found.push((((t0 + t1) / 2.0 * fps).round_ties_even() as i64, 0.0));
            }
            for (frame, strength) in found {
                if 0 < frame && frame < n && result.last().is_none_or(|last| frame > last.frame) {
                    result.push(Boundary { frame, silence: t1 - t0, scene: strength as f64 });
                }
            }
        }
        result
    }

    /// 区間のロゴの一致度の平均 (両端 EDGE_SKIP_SECONDS を除く。隅ごとの平均の最大)
    pub fn logo_mean(&self, segment: &Segment) -> Option<f64> {
        if self.logo_cumsum.is_empty() {
            return None;
        }
        let skip = (EDGE_SKIP_SECONDS * self.fps) as i64;
        let (mut a, mut b) = (segment.start + skip, segment.end - skip);
        if b <= a {
            a = segment.start;
            b = segment.end;
        }
        let n = self.logo_cumsum[0].len() as i64 - 1;
        let (a, b) = (a.max(0).min(n), b.max(0).min(n));
        if b <= a {
            return None;
        }
        let mut best = f64::NEG_INFINITY;
        for cumsum in &self.logo_cumsum {
            let mean = (cumsum[b as usize] - cumsum[a as usize]) as f64 / (b - a) as f64;
            if mean > best {
                best = mean;
            }
        }
        Some(best / 255.0)
    }

    /// 区間のうち、音量が silence_db 未満の割合
    pub fn silent_fraction(&self, segment: &Segment, t: &Thresholds) -> f64 {
        let rate = self.audio_rate;
        let a = ((segment.start as f64 / self.fps - self.audio_offset) * rate) as i64;
        let b = ((segment.end as f64 / self.fps - self.audio_offset) * rate) as i64;
        let len = self.audio_level.len();
        let (a, b) = ((a.max(0) as usize).min(len), (b.max(0) as usize).min(len));
        if b <= a {
            return 0.0;
        }
        let threshold = t.silence_db as f32;
        let quiet = (a..b).filter(|&i| self.audio_db(i) < threshold).count();
        quiet as f64 / (b - a) as f64
    }

    /// 区間ごとの判定。
    ///
    /// 1. はっきり決まるもの: 番組の前後、ロゴあり (本編)、ロゴなしで 15 秒の倍数 (CM)、CM にしては長い区間 (本編)。
    /// 2. 広げる: 決まった CM・番組の前後に隣り合い、決まった本編には隣り合わない区間を、5 秒の倍数 (番宣)・
    ///    15 秒の倍数にぴったりの長い番宣なら CM にする (決まらなくなるまで)。ロゴの出ない局でも、15 秒の倍数が
    ///    3 つ以上続く所は CM にする。
    /// 3. 残りは本編。本編と CM の間の短い区間 (提供クレジット・次回予告の見込み。利用者は残す) は確信度を低くする。
    ///
    /// ロゴの無い 5・10 秒の区間をそのまま CM にすると、本編と CM の間の提供クレジットを切った。本編の中の OP・ED
    /// (90 秒 = 15 秒 × 6) を、隣の短い区間と「CM が続く」と判定し合って切った (20 本で確認、2026-10-09)。
    pub fn classify(&self, segments: &[Segment], window: Option<(f64, f64)>, profile: Option<&Profile>,
                    t: &Thresholds) -> Vec<Decision> {
        let fps = self.fps;
        let n = segments.len();
        let logos: Vec<Option<f64>> = segments.iter().map(|s| self.logo_mean(s)).collect();
        let mut decisions: Vec<Option<Decision>> = vec![None; n];
        let decide = |i: usize, kind: Kind, confidence: f64, reason: Reason| Decision {
            segment: segments[i],
            kind,
            confidence,
            reason,
            logo: logos[i],
            short_between: false,
        };

        // 1. はっきり決まるもの
        for (i, seg) in segments.iter().enumerate() {
            let middle = (seg.start + seg.end) as f64 / 2.0 / fps;
            let logo = logos[i];
            let seconds = seg.seconds();
            if let Some((start, end)) = window
                && (middle < start || middle > end)
            {
                let reason = if middle < start { Reason::MarginBefore } else { Reason::MarginAfter };
                decisions[i] = Some(decide(i, Kind::Margin, 0.9, reason));
            } else if let Some(l) = logo.filter(|&l| l >= t.logo_program) {
                decisions[i] = Some(decide(i, Kind::Program, if l >= 0.5 { 0.9 } else { 0.7 }, Reason::LogoYes));
            } else if logo.is_some_and(|l| l <= t.logo_cm) && is_strict_cm_length(seconds, t, NO_LOGO_TOLERANCE) {
                decisions[i] = Some(decide(i, Kind::Cm, 0.9, Reason::LogoNo));
            } else if seconds > t.long_program_seconds
                && !(seconds <= t.long_cm_seconds && nearest_multiple_error(seconds, 15.0) <= SANDWICH_TOLERANCE)
            {
                decisions[i] = Some(decide(i, Kind::Program, 0.6, Reason::Long));
            }
        }

        let kind = |decisions: &Vec<Option<Decision>>, j: i64| -> Option<Kind> {
            if 0 <= j && (j as usize) < n { decisions[j as usize].map(|d| d.kind) } else { None }
        };
        let is_cut = |k: Option<Kind>| matches!(k, Some(Kind::Cm) | Some(Kind::Margin));

        // ロゴの出ない局でも、15 秒の倍数が 3 つ以上続く所は CM (境目が 0.5 秒ほどずれることがあるので ±NO_LOGO_TOLERANCE 秒まで)
        let mut i = 0;
        while i < n {
            let mut j = i;
            while j < n
                && decisions[j].is_none()
                && is_strict_cm_length(segments[j].seconds(), t, NO_LOGO_TOLERANCE)
                && logos[j].is_none_or(|l| l <= t.logo_cm)
            {
                j += 1;
            }
            if j - i >= 3 {
                for (k, d) in decisions.iter_mut().enumerate().take(j).skip(i) {
                    *d = Some(decide(k, Kind::Cm, 0.6, Reason::CmMultiples));
                }
            }
            i = j.max(i + 1);
        }

        // 2. 決まった CM・番組の前後から広げる
        let mut changed = true;
        while changed {
            changed = false;
            for (i, seg) in segments.iter().enumerate() {
                if decisions[i].is_some() {
                    continue;
                }
                let (before, after) = (kind(&decisions, i as i64 - 1), kind(&decisions, i as i64 + 1));
                let next_to_cut = is_cut(before) || is_cut(after);
                let next_to_program = before == Some(Kind::Program) || after == Some(Kind::Program);
                if !next_to_cut {
                    continue;
                }
                let logo = logos[i];
                if logo.is_some_and(|l| l > t.logo_cm) {
                    continue;
                }
                let seconds = seg.seconds();
                if (before == Some(Kind::Margin) || after == Some(Kind::Margin))
                    && seconds <= t.short_seconds
                    && is_cm_length(seconds, t)
                {
                    // 番組の前後と本編の間の短い区間 (局のジングル等。5 秒の倍数。番組の始まりの 3 秒を切ったので、
                    // 半端な長さは切らない)
                    decisions[i] = Some(decide(i, Kind::Margin, 0.5, Reason::MarginShort));
                    changed = true;
                    continue;
                }
                let silent = self.silent_fraction(seg, t);
                if silent >= t.silent_fraction && (!next_to_program || (silent >= 0.9 && seconds >= 5.0)) {
                    decisions[i] = Some(decide(i, Kind::Cm, 0.5, Reason::SilentNextToCm));
                    changed = true;
                    continue;
                }
                // 15 秒の倍数 (CM) は本編の隣でも CM にする (ロゴの出ない局で、本編の直後の CM を残した)
                if is_strict_cm_length(seconds, t, t.length_tolerance) {
                    decisions[i] = Some(decide(i, Kind::Cm, 0.7, Reason::CmContinued));
                    changed = true;
                    continue;
                }
                if next_to_program {
                    continue; // 本編と CM の間の短い区間 (提供クレジット等) は 3. で決める
                }
                // 5 秒の倍数 (局の番宣) はロゴの出る局だけ (ロゴの出ない局は長さしか手がかりが無く、番組の始まりの
                // 100 秒のアバンを切った)
                if logo.is_some() && is_cm_length(seconds, t) {
                    decisions[i] = Some(decide(i, Kind::Cm, 0.6, Reason::CmContinued));
                } else if is_long_cm_length(seconds, t) {
                    decisions[i] = Some(decide(i, Kind::Cm, 0.5, Reason::CmLongPromo));
                } else if is_cut(before)
                    && is_cut(after)
                    && seconds <= t.long_cm_seconds
                    && nearest_multiple_error(seconds, 15.0) <= SANDWICH_TOLERANCE
                {
                    decisions[i] = Some(decide(i, Kind::Cm, 0.4, Reason::CmSandwichedPromo));
                } else {
                    continue;
                }
                changed = true;
            }
        }

        // 3. 残りは本編 (前の区間の判定はこのループで決めたものを使う)
        for (i, seg) in segments.iter().enumerate() {
            if decisions[i].is_some() {
                continue;
            }
            let (before, after) = (kind(&decisions, i as i64 - 1), kind(&decisions, i as i64 + 1));
            let between =
                (before == Some(Kind::Program) && is_cut(after)) || (after == Some(Kind::Program) && is_cut(before));
            if between && seg.seconds() <= t.short_seconds {
                let kept = profile.and_then(|p| p.short_between_kept);
                let mut d = if kept.is_some_and(|k| k < 0.5) && logos[i].is_none_or(|l| l <= t.logo_cm) {
                    decide(i, Kind::Cm, 0.5, Reason::ShortBetweenCut)
                } else {
                    decide(i, Kind::Program, 0.3, Reason::ShortBetweenKept)
                };
                d.short_between = true;
                decisions[i] = Some(d);
            } else {
                let confidence = if logos[i].is_none() { 0.6 } else { 0.4 };
                decisions[i] = Some(decide(i, Kind::Program, confidence, Reason::NotCm));
            }
        }
        decisions.into_iter().map(|d| d.unwrap()).collect()
    }

    /// 境目で区切り、短い区間を手前につないだ区間
    pub fn segments(&self, boundaries: &[Boundary], t: &Thresholds) -> Vec<Segment> {
        merge_short_segments(&split_segments(boundaries, self.frame_count, self.fps), t)
    }

    /// 残す区間の終わりが、次の境目 (新しい場面) の何フレーム手前だったか
    pub fn measure_end_offsets(&self, truth: &[(i64, i64, bool)], boundaries: &[Boundary]) -> Vec<i64> {
        let mut result = Vec::new();
        for &(_start, end, excluded) in truth {
            if excluded || end >= self.frame_count - 1 || boundaries.is_empty() {
                continue;
            }
            let found = boundaries
                .iter()
                .map(|b| b.frame - end)
                .filter(|&d| (0..=END_MATCH_FRAMES).contains(&d))
                .min();
            if let Some(d) = found {
                result.push(d);
            }
        }
        result
    }

    /// ルールで「本編と CM の間の短い区間」になった区間を、利用者が残したか (区間ごと)
    pub fn measure_short_between(&self, truth: &[(i64, i64, bool)], window: Option<(f64, f64)>,
                                 boundaries: &[Boundary], t: &Thresholds) -> Vec<bool> {
        let segments = self.segments(boundaries, t);
        let mut result = Vec::new();
        for d in self.classify(&segments, window, None, t) {
            if d.kind != Kind::Program || !d.short_between {
                continue;
            }
            let seg = d.segment;
            let kept: i64 =
                truth.iter().filter(|r| !r.2).map(|&(a, b, _)| (b.min(seg.end) - a.max(seg.start)).max(0)).sum();
            let excluded = truth.iter().any(|&(a, b, ex)| ex && a < seg.end && seg.start < b);
            if !excluded {
                result.push(kept as f64 >= (seg.end - seg.start) as f64 / 2.0);
            }
        }
        result
    }

    /// 残す区間の案の区間 (start, end)
    pub fn propose(&self, window: Option<(f64, f64)>, profile: &Profile, boundaries: &[Boundary],
                   t: &Thresholds) -> Vec<(i64, i64)> {
        if self.frame_count == 0 {
            return Vec::new();
        }
        let segments = self.segments(boundaries, t);
        let kinds: Vec<Kind> = self.classify(&segments, window, Some(profile), t).iter().map(|d| d.kind).collect();
        assemble(&kinds, &segments, profile.end_offset).into_iter().map(|r| (r.start, r.end)).collect()
    }
}

/// 境目で区切った区間 (録画全体を覆う)
pub fn split_segments(boundaries: &[Boundary], frame_count: i64, fps: f64) -> Vec<Segment> {
    let mut cuts = vec![0];
    cuts.extend(boundaries.iter().map(|b| b.frame));
    cuts.push(frame_count);
    cuts.windows(2).filter(|w| w[1] > w[0]).map(|w| Segment { start: w[0], end: w[1], fps }).collect()
}

/// min_segment_seconds 秒より短い区間を手前の区間につなげる (CM なら「CM 本体＋後ろの黒い画面・無音」で 15 秒の
/// 倍数に揃う)。最初の区間はそのまま。録画の最後の短い区間も手前につなげると、番組の前後の区間が消えて手前の CM を
/// 残す案になったので、そのまま
pub fn merge_short_segments(segments: &[Segment], t: &Thresholds) -> Vec<Segment> {
    let mut result: Vec<Segment> = Vec::new();
    for (k, seg) in segments.iter().enumerate() {
        let last = k == segments.len() - 1;
        if !result.is_empty() && seg.seconds() < t.min_segment_seconds && !last {
            let prev = result.pop().unwrap();
            result.push(Segment { start: prev.start, end: seg.end, fps: seg.fps });
        } else {
            result.push(*seg);
        }
    }
    result
}

/// 案 1 つ: 区間と、つないだ判定の番号 [first, last)、直前・直後の切る区間の判定の番号
pub struct Run {
    pub start: i64,
    pub end: i64,
    pub first: usize,
    pub last: usize,
    pub prev_cut: Option<usize>,
    pub next_cut: Option<usize>,
}

/// 区間ごとの判定 (ルール・学習 A8) から、本編の区間をつないだ残す区間の案。次が切る区間なら、
/// 境目の end_offset フレーム手前で終える (区間の始まりより 1 フレームは後)
pub fn assemble(kinds: &[Kind], segments: &[Segment], end_offset: i64) -> Vec<Run> {
    let mut runs = Vec::new();
    let mut first: Option<usize> = None;
    let mut prev_cut: Option<usize> = None;
    let close = |runs: &mut Vec<Run>, first: usize, last: usize, next_cut: Option<usize>, prev_cut: Option<usize>| {
        let start = segments[first].start;
        let mut end = segments[last - 1].end;
        if next_cut.is_some() {
            end = (end - end_offset).max(start + 1);
        }
        runs.push(Run { start, end, first, last, prev_cut, next_cut });
    };
    for (i, &kind) in kinds.iter().enumerate() {
        if kind == Kind::Program {
            first.get_or_insert(i);
            continue;
        }
        if let Some(f) = first.take() {
            close(&mut runs, f, i, Some(i), prev_cut);
        }
        prev_cut = Some(i);
    }
    if let Some(f) = first {
        close(&mut runs, f, kinds.len(), None, prev_cut);
    }
    runs
}

/// 測った材料 (録画, 区間の終わりの差, 短い区間を残したか) から局の値。exclude_recording の録画の材料は使わない
pub fn profile_from_measures(measures: &[(i64, Vec<i64>, Vec<bool>)], exclude_recording: Option<i64>) -> Profile {
    let mut offsets: Vec<i64> = Vec::new();
    let mut between: Vec<bool> = Vec::new();
    for (recording_id, o, b) in measures {
        if Some(*recording_id) != exclude_recording {
            offsets.extend(o);
            between.extend(b);
        }
    }
    let end_offset = if offsets.is_empty() {
        DEFAULT_END_OFFSET
    } else {
        // 中央値を整数に (偶数個なら真ん中の 2 つの平均を 0 の方へ切り捨て)
        offsets.sort();
        let m = offsets.len() / 2;
        let median =
            if offsets.len() % 2 == 1 { offsets[m] as f64 } else { (offsets[m - 1] as f64 + offsets[m] as f64) / 2.0 };
        median.trunc() as i64
    };
    // 1〜2 例では局の癖とは言えない (BS-C は 2 本で残す・切るが分かれた)
    let short_between_kept = if between.len() >= MIN_SHORT_BETWEEN_SAMPLES {
        Some(between.iter().filter(|&&b| b).count() as f64 / between.len() as f64)
    } else {
        None
    };
    Profile { end_offset, short_between_kept }
}

// ---- 正解との比較 (evaluate.compare) ------------------------------------------------

/// evaluate.Score の値 (truth_cut, proposed_cut, correct_cut, wrong_cut, ±0, ±2f, ±1秒, 外れ, extra_edges, recordings)
#[derive(Default)]
pub struct Score {
    pub truth_cut: f64,
    pub proposed_cut: f64,
    pub correct_cut: f64,
    pub wrong_cut: f64,
    pub edges: [i64; 4],
    pub extra_edges: i64,
    pub recordings: i64,
}

pub type ScoreTuple = (f64, f64, f64, f64, i64, i64, i64, i64, i64, i64);

impl Score {
    pub fn add(&mut self, other: &Score) {
        self.truth_cut += other.truth_cut;
        self.proposed_cut += other.proposed_cut;
        self.correct_cut += other.correct_cut;
        self.wrong_cut += other.wrong_cut;
        for k in 0..4 {
            self.edges[k] += other.edges[k];
        }
        self.extra_edges += other.extra_edges;
        self.recordings += other.recordings;
    }

    pub fn tuple(&self) -> ScoreTuple {
        (self.truth_cut, self.proposed_cut, self.correct_cut, self.wrong_cut, self.edges[0], self.edges[1],
         self.edges[2], self.edges[3], self.extra_edges, self.recordings)
    }
}

/// 正解の区間 (録画ごとに 1 回だけ作る)。どれも [0, フレーム数) に切り詰め、重なりをまとめて並べたもの
pub struct TruthSpans {
    counted: Vec<(i64, i64)>, // 除く区間 (excluded) の外
    cut: Vec<(i64, i64)>,     // 除く区間の外で、正解で切る所
    keep: Vec<(i64, i64)>,    // 除く区間の外で、正解で残す所
    edges: Vec<i64>,          // 正解の境目 (残す区間の始まり・終わり。録画の先頭・末尾は除く)
}

impl TruthSpans {
    pub fn new(truth: &[(i64, i64, bool)], frame_count: i64) -> TruthSpans {
        let n = frame_count.max(0);
        let excluded = spans(truth.iter().filter(|r| r.2).map(|r| (r.0, r.1)), n);
        let keep = spans(truth.iter().filter(|r| !r.2).map(|r| (r.0, r.1)), n);
        let counted = complement(&excluded, n);
        let cut = subtract(&counted, &keep);
        TruthSpans {
            keep: subtract(&keep, &excluded),
            counted,
            cut,
            edges: edges(truth.iter().filter(|r| !r.2).map(|r| (r.0, r.1)), frame_count),
        }
    }
}

/// ranges を [0, n) に切り詰め、重なり・隣り合いをまとめて並べる
fn spans(ranges: impl Iterator<Item = (i64, i64)>, n: i64) -> Vec<(i64, i64)> {
    let mut clipped: Vec<(i64, i64)> =
        ranges.map(|(a, b)| (a.max(0).min(n), b.max(0).min(n))).filter(|(a, b)| a < b).collect();
    clipped.sort();
    let mut result: Vec<(i64, i64)> = Vec::new();
    for (a, b) in clipped {
        match result.last_mut() {
            Some(last) if a <= last.1 => last.1 = last.1.max(b),
            _ => result.push((a, b)),
        }
    }
    result
}

/// [0, n) から spans を除いた所
fn complement(spans: &[(i64, i64)], n: i64) -> Vec<(i64, i64)> {
    let mut result = Vec::new();
    let mut at = 0;
    for &(a, b) in spans {
        if at < a {
            result.push((at, a));
        }
        at = b;
    }
    if at < n {
        result.push((at, n));
    }
    result
}

/// a から b を除いた所 (どちらも spans の形)
fn subtract(a: &[(i64, i64)], b: &[(i64, i64)]) -> Vec<(i64, i64)> {
    let mut result = Vec::new();
    let mut j = 0;
    for &(start, end) in a {
        let mut at = start;
        while j < b.len() && b[j].1 <= at {
            j += 1;
        }
        let mut k = j;
        while k < b.len() && b[k].0 < end {
            if at < b[k].0 {
                result.push((at, b[k].0));
            }
            at = at.max(b[k].1);
            k += 1;
        }
        if at < end {
            result.push((at, end));
        }
    }
    result
}

fn total(spans: &[(i64, i64)]) -> i64 {
    spans.iter().map(|(a, b)| b - a).sum()
}

/// a と b (どちらも spans の形) の重なりのフレーム数
fn overlap(a: &[(i64, i64)], b: &[(i64, i64)]) -> i64 {
    let (mut i, mut j, mut sum) = (0, 0, 0);
    while i < a.len() && j < b.len() {
        let (start, end) = (a[i].0.max(b[j].0), a[i].1.min(b[j].1));
        if start < end {
            sum += end - start;
        }
        if a[i].1 < b[j].1 {
            i += 1;
        } else {
            j += 1;
        }
    }
    sum
}

fn edges(ranges: impl Iterator<Item = (i64, i64)>, n: i64) -> Vec<i64> {
    let mut result = Vec::new();
    for (a, b) in ranges {
        if a > 0 {
            result.push(a);
        }
        if b < n {
            result.push(b);
        }
    }
    result.sort();
    result
}

/// 案と正解の比較。切る時間は「除く区間」の外で数え、境目は正解の境目ごとにいちばん近い案の境目との差
/// (±0・±2 フレーム以内・±1 秒以内・外れ) と、±1 秒以内に正解の境目が無い案の境目の数。
/// フレームごとの印で数えるのと同じ数を、区間の重なりで数える (2026-10-27)
pub fn compare(truth: &TruthSpans, proposals: &[(i64, i64)], frame_count: i64, fps: f64) -> Score {
    let proposed_keep = spans(proposals.iter().copied(), frame_count.max(0));
    let truth_cut = total(&truth.cut);
    let proposed_cut = total(&truth.counted) - overlap(&truth.counted, &proposed_keep);
    let correct_cut = truth_cut - overlap(&truth.cut, &proposed_keep);
    let wrong_cut = total(&truth.keep) - overlap(&truth.keep, &proposed_keep);
    let mut score = Score {
        truth_cut: truth_cut as f64 / fps,
        proposed_cut: proposed_cut as f64 / fps,
        correct_cut: correct_cut as f64 / fps,
        wrong_cut: wrong_cut as f64 / fps,
        recordings: 1,
        ..Score::default()
    };
    let truth_edges = &truth.edges;
    let proposed_edges = edges(proposals.iter().copied(), frame_count);
    let second = fps.round_ties_even() as i64;
    for &e in truth_edges {
        let d = proposed_edges.iter().map(|&p| (p - e).abs()).min().unwrap_or(1_000_000_000);
        let key = if d == 0 {
            0
        } else if d <= 2 {
            1
        } else if d <= second {
            2
        } else {
            3
        };
        score.edges[key] += 1;
    }
    for &e in &proposed_edges {
        if truth_edges.is_empty() || truth_edges.iter().map(|&t| (t - e).abs()).min().unwrap() > second {
            score.extra_edges += 1;
        }
    }
    score
}

// ---- Python から呼ぶ ----------------------------------------------------------

/// 録画 1 本の特徴 (Python の rules._recording が RecordingFeatures から 1 回だけ作る)。
/// audio_level は uint8 (0.5dB 刻み)、scene は float16 のバイト列 (little endian)、logo_scores はロゴのある隅の
/// 一致度 (uint8、隅の数 × フレーム数。ロゴの無い局は None)
#[pyclass(module = "ts_smartcut_native", frozen)]
pub struct RuleRecording {
    pub inner: Arc<Recording>,
}

#[pymethods]
impl RuleRecording {
    #[new]
    #[pyo3(signature = (fps, frame_count, audio_rate, audio_offset, audio_level, scene, logo_scores, logo_corners))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        fps: f64,
        frame_count: i64,
        audio_rate: f64,
        audio_offset: f64,
        audio_level: &Bound<'_, PyBytes>,
        scene: &Bound<'_, PyBytes>,
        logo_scores: Option<&Bound<'_, PyBytes>>,
        logo_corners: usize,
    ) -> RuleRecording {
        let mut logo_cumsum = Vec::new();
        if let Some(data) = logo_scores
            && logo_corners > 0
        {
            let bytes = data.as_bytes();
            let width = bytes.len() / logo_corners;
            for corner in bytes.chunks_exact(width) {
                let mut cumsum = Vec::with_capacity(width + 1);
                let mut sum: i32 = 0;
                cumsum.push(0);
                for &v in corner {
                    sum += v as i32;
                    cumsum.push(sum);
                }
                logo_cumsum.push(cumsum);
            }
        }
        let scene: Vec<u16> =
            scene.as_bytes().as_chunks::<2>().0.iter().map(|c| u16::from_le_bytes([c[0], c[1]])).collect();
        RuleRecording {
            inner: Arc::new(Recording {
                fps,
                frame_count,
                audio_rate,
                audio_offset,
                audio_level: audio_level.as_bytes().to_vec(),
                scene,
                logo_cumsum,
            }),
        }
    }
}

type BoundaryTuple = (i64, f64, f64);
/// (種類, 確信度, 理由, ロゴの一致度, 本編と CM の間の短い区間か)
type DecisionTuple = (&'static str, f64, &'static str, Option<f64>, bool);

fn to_boundaries(values: &[BoundaryTuple]) -> Vec<Boundary> {
    values.iter().map(|&(frame, silence, scene)| Boundary { frame, silence, scene }).collect()
}

fn to_segments(values: &[(i64, i64)], fps: f64) -> Vec<Segment> {
    values.iter().map(|&(start, end)| Segment { start, end, fps }).collect()
}

fn to_profile(value: Option<(i64, Option<f64>)>) -> Option<Profile> {
    value.map(|(end_offset, short_between_kept)| Profile { end_offset, short_between_kept })
}

/// 境目の候補 (frame, 無音の秒, 場面の切り替わり)
#[pyfunction]
pub fn rule_boundaries(recording: &RuleRecording, thresholds: Vec<f64>) -> PyResult<Vec<BoundaryTuple>> {
    let t = Thresholds::from_values(&thresholds)?;
    Ok(recording.inner.find_boundaries(&t).iter().map(|b| (b.frame, b.silence, b.scene)).collect())
}

/// 境目で区切り、短い区間を手前につないだ区間 (start, end)
#[pyfunction]
pub fn rule_segments(recording: &RuleRecording, boundaries: Vec<BoundaryTuple>, thresholds: Vec<f64>)
                     -> PyResult<Vec<(i64, i64)>> {
    let t = Thresholds::from_values(&thresholds)?;
    Ok(recording.inner.segments(&to_boundaries(&boundaries), &t).iter().map(|s| (s.start, s.end)).collect())
}

/// 区間ごとの判定。window は放送時間 (秒)、profile は局の値 (end_offset, short_between_kept)
#[pyfunction]
#[pyo3(signature = (recording, segments, window, profile, thresholds))]
pub fn rule_classify(recording: &RuleRecording, segments: Vec<(i64, i64)>, window: Option<(f64, f64)>,
                     profile: Option<(i64, Option<f64>)>, thresholds: Vec<f64>) -> PyResult<Vec<DecisionTuple>> {
    let t = Thresholds::from_values(&thresholds)?;
    let rec = &recording.inner;
    let profile = to_profile(profile);
    Ok(rec
        .classify(&to_segments(&segments, rec.fps), window, profile.as_ref(), &t)
        .iter()
        .map(|d| (d.kind.name(), d.confidence, d.reason.name(), d.logo, d.short_between))
        .collect())
}

/// 区間ごとの (ロゴの一致度の平均, 無音の割合) (学習 A8 の区間の特徴)
#[pyfunction]
pub fn rule_segment_values(recording: &RuleRecording, segments: Vec<(i64, i64)>, thresholds: Vec<f64>)
                           -> PyResult<Vec<(Option<f64>, f64)>> {
    let t = Thresholds::from_values(&thresholds)?;
    let rec = &recording.inner;
    Ok(to_segments(&segments, rec.fps).iter().map(|s| (rec.logo_mean(s), rec.silent_fraction(s, &t))).collect())
}

/// 案 (start, end, つないだ判定 [first, last), 直前の切る区間, 直後の切る区間)
type RunTuple = (i64, i64, usize, usize, Option<usize>, Option<usize>);

/// 区間ごとの判定の種類から案 (RunTuple)
#[pyfunction]
pub fn rule_assemble(kinds: Vec<String>, segments: Vec<(i64, i64)>, end_offset: i64)
                     -> PyResult<Vec<RunTuple>> {
    let kinds = kinds.iter().map(|k| Kind::from_name(k)).collect::<PyResult<Vec<Kind>>>()?;
    let segments = to_segments(&segments, 1.0);
    Ok(assemble(&kinds, &segments, end_offset)
        .into_iter()
        .map(|r| (r.start, r.end, r.first, r.last, r.prev_cut, r.next_cut))
        .collect())
}

/// 局の値の材料 (区間の終わりの差, 短い区間を残したか)。boundaries は同じ thresholds で求めた境目の候補
#[pyfunction]
pub fn rule_measures(recording: &RuleRecording, truth: Vec<(i64, i64, bool)>, window: Option<(f64, f64)>,
                     boundaries: Vec<BoundaryTuple>, thresholds: Vec<f64>) -> PyResult<(Vec<i64>, Vec<bool>)> {
    let t = Thresholds::from_values(&thresholds)?;
    let boundaries = to_boundaries(&boundaries);
    let rec = &recording.inner;
    Ok((rec.measure_end_offsets(&truth, &boundaries), rec.measure_short_between(&truth, window, &boundaries, &t)))
}

/// 測った材料 [(録画, 区間の終わりの差, 短い区間を残したか)] から局の値 (end_offset, short_between_kept)
#[pyfunction]
#[pyo3(signature = (measures, exclude_recording=None))]
pub fn rule_profile(measures: Vec<(i64, Vec<i64>, Vec<bool>)>, exclude_recording: Option<i64>) -> (i64, Option<f64>) {
    let p = profile_from_measures(&measures, exclude_recording);
    (p.end_offset, p.short_between_kept)
}

/// 案と正解の比較 (evaluate.Score の値)
#[pyfunction]
pub fn rule_compare(truth: Vec<(i64, i64, bool)>, proposals: Vec<(i64, i64)>, frame_count: i64, fps: f64)
                    -> ScoreTuple {
    compare(&TruthSpans::new(&truth, frame_count), &proposals, frame_count, fps).tuple()
}
