//! 特徴の計算で、録画を読み終わった後の計算 (設計書 §31.4、自動カット計画 A3・A4)。2026-10-28 までは Python
//! (features.py・logo.py・logo_image.py・core/waveform.py の numpy)。
//!
//! - 音量: 8kHz の音声の 10ms ごとのピーク → 0.5dB 刻みの uint8 (画面の波形もピークはここ)
//! - コマとフレームの対応: 表示時刻が戻ったコマを除き、フレーム N を「表示時刻 >= 映像開始 + (N-0.5)/fps の
//!   最初のコマ」にする (カット処理の _boundary と同じ)。場面の切り替わりはコマの最初のフレームだけ
//! - ロゴの型 (logo.py の説明): 縮小した四隅をさらに 1/2 にし、残した区間のフレームの縁の向きを数え、3 割以上で
//!   同じ向きの画素をマスクにする。一致度はマスクの画素で縁の向きが型と合う割合 (型の割合の重み付き)
//! - 四隅の平均と標準偏差、ロゴ画像の材料 (logo_image.py の説明)
//!
//! 値は numpy の時と同じ (float32 の合計は numpy と同じペアワイズ和、float16 への丸めは最も近い偶数)。違うのは
//! 2 つだけ (2026-10-28 利用者の了承): ロゴの一致度の重み付きの和 (numpy は BLAS で足す順が決まらない。保存する
//! 0〜255 が変わるのはまれに ±1)、ロゴ画像の材料の float64 の合計の最後の桁。

use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::fs::File;
use std::io::{BufReader, Read};

// ロゴの型: 縁とみなす明るさの差、マスクにする割合、ロゴがある隅とみなすマスクの画素数
const EDGE: i16 = 2;
pub(crate) const CONSISTENCY: f32 = 0.3;
const MIN_MASK_PIXELS: usize = 8;
// 音量の保存形式: dB を 0.5dB 刻みの uint8 にする。値 q は q/2 - 100 dB
const LEVEL_FLOOR_DB: f64 = -100.0;
const SAMPLE_RATE: usize = 8000;
const ENVELOPE_RATE: usize = 100;

// ---- 数値の扱い (numpy と同じにする) ------------------------------------------------

/// float64 を float16 のビットにする (最も近い偶数への丸め。numpy の astype(float16) と同じ)
pub fn f64_to_f16(x: f64) -> u16 {
    let sign: u16 = if x.is_sign_negative() { 0x8000 } else { 0 };
    let a = x.abs();
    if x.is_nan() {
        return sign | 0x7E00;
    }
    if a >= 65520.0 {
        return sign | 0x7C00; // 65520 以上は無限大に丸まる
    }
    if a < 6.103515625e-05 {
        // 非正規化数: 2^-24 単位の整数に丸める (1024 になれば最小の正規化数と同じビット)
        let k = (a * 16777216.0).round_ties_even();
        return sign | k as u16;
    }
    let bits = a.to_bits();
    let exponent = ((bits >> 52) & 0x7FF) as i32 - 1023;
    let mantissa = (bits & ((1u64 << 52) - 1)) | (1u64 << 52);
    let mut m = mantissa >> 42;
    let rest = mantissa & ((1u64 << 42) - 1);
    let half = 1u64 << 41;
    if rest > half || (rest == half && m & 1 == 1) {
        m += 1;
    }
    let mut e = exponent + 15;
    if m == 2048 {
        m = 1024;
        e += 1;
    }
    if e >= 31 {
        return sign | 0x7C00;
    }
    sign | ((e as u16) << 10) | (m as u16 & 0x3FF)
}

/// numpy の float32 の合計 (np.add.reduce のペアワイズ和) と同じ順に足す
pub fn pairwise_sum_f32(a: &[f32]) -> f32 {
    let n = a.len();
    if n < 8 {
        let mut res = 0.0f32;
        for &x in a {
            res += x;
        }
        res
    } else if n <= 128 {
        let mut r = [0f32; 8];
        r.copy_from_slice(&a[..8]);
        let mut i = 8;
        while i < n - n % 8 {
            for j in 0..8 {
                r[j] += a[i + j];
            }
            i += 8;
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        while i < n {
            res += a[i];
            i += 1;
        }
        res
    } else {
        let mut n2 = n / 2;
        n2 -= n2 % 8;
        pairwise_sum_f32(&a[..n2]) + pairwise_sum_f32(&a[n2..])
    }
}

pub(crate) fn bytes_f32(data: &[u8]) -> Vec<f32> {
    data.as_chunks::<4>().0.iter().map(|c| f32::from_le_bytes(*c)).collect()
}

pub(crate) fn bytes_f64(data: &[u8]) -> Vec<f64> {
    data.as_chunks::<8>().0.iter().map(|c| f64::from_le_bytes(*c)).collect()
}

fn bytes_i64(data: &[u8]) -> Vec<i64> {
    data.as_chunks::<8>().0.iter().map(|c| i64::from_le_bytes(*c)).collect()
}

pub(crate) fn to_bytes<'py, T: Copy>(py: Python<'py>, values: &[T], f: impl Fn(T) -> Vec<u8>) -> Bound<'py, PyBytes> {
    let mut out = Vec::with_capacity(std::mem::size_of_val(values));
    for &v in values {
        out.extend(f(v));
    }
    PyBytes::new(py, &out)
}

// ---- 音量 -----------------------------------------------------------------

/// int16 の音声を、per_point サンプルごとの絶対値の最大 (32767 まで) にする
fn peaks(samples: &[i16], per_point: usize) -> Vec<i16> {
    let per_point = per_point.max(1);
    samples.chunks_exact(per_point).map(|b| b.iter().map(|&s| (s as i32).abs()).max().unwrap().min(32767) as i16).collect()
}

/// ピーク (int16 の目盛り) を 0.5dB 刻みの uint8 にする
fn level(peak: i16) -> u8 {
    let p = (peak as f64).max(0.0);
    if p == 0.0 {
        return 0;
    }
    let db = 20.0 * (p / 32767.0).log10();
    ((db - LEVEL_FLOOR_DB) * 2.0).round_ties_even().clamp(0.0, 255.0) as u8
}

fn read_i16(path: &str) -> std::io::Result<Vec<i16>> {
    let mut data = Vec::new();
    BufReader::new(File::open(path)?).read_to_end(&mut data)?;
    Ok(data.as_chunks::<2>().0.iter().map(|c| i16::from_le_bytes(*c)).collect())
}

/// 音声 (int16 のバイト列) の 1/rate 秒ごとのピーク (int16 のバイト列)
#[pyfunction]
pub fn audio_peaks<'py>(py: Python<'py>, samples: &[u8], rate: usize) -> Bound<'py, PyBytes> {
    let samples: Vec<i16> = samples.as_chunks::<2>().0.iter().map(|c| i16::from_le_bytes(*c)).collect();
    let p = peaks(&samples, SAMPLE_RATE / rate.max(1));
    to_bytes(py, &p, |v: i16| v.to_le_bytes().to_vec())
}

/// ピーク (int16 のバイト列) を保存形式の音量 (uint8) にする
#[pyfunction]
pub fn peaks_to_levels<'py>(py: Python<'py>, peaks: &[u8]) -> Bound<'py, PyBytes> {
    let values: Vec<u8> = peaks.as_chunks::<2>().0.iter().map(|c| level(i16::from_le_bytes(*c))).collect();
    PyBytes::new(py, &values)
}

/// 特徴の計算の一時ファイル (8kHz モノラル int16) から、10ms ごとの音量 (uint8)
#[pyfunction]
pub fn audio_levels_from_file<'py>(py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyBytes>> {
    let values = py.detach(|| -> std::io::Result<Vec<u8>> {
        let samples = read_i16(path)?;
        Ok(peaks(&samples, SAMPLE_RATE / ENVELOPE_RATE).into_iter().map(level).collect())
    })?;
    Ok(PyBytes::new(py, &values))
}

// ---- コマとフレーム -------------------------------------------------------

/// 表示時刻がそれまでのどのコマより後のコマの番号 (時刻が戻った・同じ時刻のコマを除く)
fn increasing(pts: &[f64]) -> Vec<usize> {
    let mut result = Vec::new();
    let mut highest = f64::NEG_INFINITY;
    for (i, &t) in pts.iter().enumerate() {
        if i == 0 || t > highest {
            result.push(i);
        }
        if i == 0 || t > highest {
            highest = t;
        }
    }
    result
}

/// コマの表示時刻 (昇順) から、フレーム番号ごとのコマの番号と、そのフレームがコマの最初のフレームか
fn frame_map(pts: &[f64], video_start: f64, fps: f64) -> (Vec<usize>, Vec<bool>) {
    if pts.is_empty() {
        return (Vec::new(), Vec::new());
    }
    let count = (((pts[pts.len() - 1] - video_start) * fps + 0.5).floor() as i64 + 1).max(0) as usize;
    let offset = 0.25 / 90000.0;
    let mut idx = Vec::with_capacity(count);
    let mut first = Vec::with_capacity(count);
    let mut j = 0;
    for k in 0..count {
        // 時刻の丸め (1/90000 秒) で境目ちょうどのコマを落とさないよう、少しだけ早める
        let target = video_start + (k as f64 - 0.5) / fps - offset;
        // searchsorted(side="left"): target 以上の最初のコマ (target は増えるので前から進める)
        while j < pts.len() && pts[j] < target {
            j += 1;
        }
        let i = j.min(pts.len() - 1);
        first.push(k == 0 || i != idx[k - 1]);
        idx.push(i);
    }
    (idx, first)
}

/// 表示時刻が戻ったコマを除いたコマの番号 (int64 のバイト列)
#[pyfunction]
pub fn increasing_pictures<'py>(py: Python<'py>, pts: &[u8]) -> Bound<'py, PyBytes> {
    let positions = increasing(&bytes_f64(pts));
    to_bytes(py, &positions, |v: usize| (v as i64).to_le_bytes().to_vec())
}

/// (フレームごとのコマの番号 (int64)、コマの最初のフレームか (uint8))
#[pyfunction]
pub fn map_pictures_to_frames<'py>(py: Python<'py>, pts: &[u8], video_start: f64, fps: f64)
                                   -> (Bound<'py, PyBytes>, Bound<'py, PyBytes>) {
    let (idx, first) = frame_map(&bytes_f64(pts), video_start, fps);
    (to_bytes(py, &idx, |v: usize| (v as i64).to_le_bytes().to_vec()),
     PyBytes::new(py, &first.iter().map(|&b| b as u8).collect::<Vec<u8>>()))
}

/// 読み込みのコマごとの値から、フレームごとの値: (フレームごとのコマの番号 (int64)、場面の切り替わり (float16。
/// コマの最初のフレームだけ、0 番は 0)、時刻が戻って飛ばしたコマの数、その最初のコマ)
#[pyfunction]
pub fn picture_frames<'py>(py: Python<'py>, pts: &[u8], diffs: &[u8], video_start: f64, fps: f64)
                           -> (Bound<'py, PyBytes>, Bound<'py, PyBytes>, usize, Option<usize>) {
    let pts = bytes_f64(pts);
    let diffs = bytes_f32(diffs);
    let count = pts.len().min(diffs.len());
    let positions = increasing(&pts[..count]);
    let skipped = count - positions.len();
    let first_skipped = if skipped > 0 { (0..count).find(|i| positions.binary_search(i).is_err()) } else { None };
    let kept: Vec<f64> = positions.iter().map(|&p| pts[p]).collect();
    let (kept_idx, first) = frame_map(&kept, video_start, fps);
    let idx: Vec<usize> = kept_idx.iter().map(|&k| positions[k]).collect();
    let mut scene: Vec<u16> = idx
        .iter()
        .zip(&first)
        .map(|(&i, &f)| f64_to_f16(if f { diffs[i] as f64 } else { 0.0 }))
        .collect();
    if let Some(s) = scene.first_mut() {
        *s = 0;
    }
    (to_bytes(py, &idx, |v: usize| (v as i64).to_le_bytes().to_vec()),
     to_bytes(py, &scene, |v: u16| v.to_le_bytes().to_vec()), skipped, first_skipped)
}

/// 四隅の合計と 2 乗の合計 (float64) とフレーム数から、平均と標準偏差 (float16。フレームが無ければ 0)
#[pyfunction]
pub fn corner_mean_std<'py>(py: Python<'py>, total: &[u8], square: &[u8], n: u64)
                            -> (Bound<'py, PyBytes>, Bound<'py, PyBytes>) {
    let total = bytes_f64(total);
    let square = bytes_f64(square);
    let (mut mean, mut std) = (Vec::with_capacity(total.len()), Vec::with_capacity(total.len()));
    for (&t, &s) in total.iter().zip(&square) {
        if n == 0 {
            mean.push(0u16);
            std.push(0u16);
            continue;
        }
        let m = t / n as f64;
        let var = (s / n as f64 - m * m).max(0.0);
        mean.push(f64_to_f16(m));
        std.push(f64_to_f16(var.sqrt()));
    }
    (to_bytes(py, &mean, |v: u16| v.to_le_bytes().to_vec()), to_bytes(py, &std, |v: u16| v.to_le_bytes().to_vec()))
}

// ---- ロゴの型と一致度 -------------------------------------------------------

/// ロゴの型 (隅ごとの縁の向きの割合とマスク)。gx (4, h, w-1)、gy (4, h-1, w)
pub(crate) struct Template {
    pub(crate) h: usize,
    pub(crate) w: usize,
    pub(crate) gx: Vec<f32>,
    pub(crate) gy: Vec<f32>,
    pub(crate) mask_x: Vec<bool>,
    pub(crate) mask_y: Vec<bool>,
}

impl Template {
    pub(crate) fn x_len(&self) -> usize {
        self.h * self.w.saturating_sub(1)
    }

    pub(crate) fn y_len(&self) -> usize {
        self.h.saturating_sub(1) * self.w
    }

    pub(crate) fn has_logo(&self, c: usize) -> bool {
        let (xl, yl) = (self.x_len(), self.y_len());
        let pixels = self.mask_x[c * xl..(c + 1) * xl].iter().filter(|&&m| m).count()
            + self.mask_y[c * yl..(c + 1) * yl].iter().filter(|&&m| m).count();
        pixels >= MIN_MASK_PIXELS
    }
}

/// 縁の向き: 差が EDGE 以上なら +1、-EDGE 以下なら -1、それ以外は 0
fn sign(d: i16) -> i8 {
    (d >= EDGE) as i8 - (d <= -EDGE) as i8
}

/// 縮小した四隅 (4, H, W) を縦横 1/2 (2×2 の平均、四捨五入) にする。端の奇数の行・列は捨てる
pub(crate) fn downscale(frame: &[u8], big_h: usize, big_w: usize, out: &mut Vec<u8>) {
    let (h, w) = (big_h / 2, big_w / 2);
    out.clear();
    for c in 0..4 {
        let base = c * big_h * big_w;
        for y in 0..h {
            for x in 0..w {
                let at = |yy: usize, xx: usize| frame[base + yy * big_w + xx] as u16;
                let total = at(2 * y, 2 * x) + at(2 * y + 1, 2 * x) + at(2 * y, 2 * x + 1) + at(2 * y + 1, 2 * x + 1);
                out.push(((total + 2) / 4) as u8);
            }
        }
    }
}

/// 縮小した四隅 1 フレーム (4, h, w) の縁の向きを sum_x・sum_y に足す
pub(crate) fn add_signs(small: &[u8], h: usize, w: usize, sum_x: &mut [i64], sum_y: &mut [i64]) {
    let (xl, yl) = (h * w.saturating_sub(1), h.saturating_sub(1) * w);
    for c in 0..4 {
        let px = |y: usize, x: usize| small[c * h * w + y * w + x] as i16;
        for y in 0..h {
            for x in 0..w.saturating_sub(1) {
                sum_x[c * xl + y * (w - 1) + x] += sign(px(y, x + 1) - px(y, x)) as i64;
            }
        }
        for y in 0..h.saturating_sub(1) {
            for x in 0..w {
                sum_y[c * yl + y * w + x] += sign(px(y + 1, x) - px(y, x)) as i64;
            }
        }
    }
}

/// 割合 gx・gy から型: マスクは |g| >= CONSISTENCY (フレームが無ければ空)、強さはマスクの |g| の平均
pub(crate) fn from_consistency(h: usize, w: usize, gx: Vec<f32>, gy: Vec<f32>, count: u64) -> (Template, [f32; 4]) {
    let mask_x: Vec<bool> = gx.iter().map(|&g| count > 0 && g.abs() >= CONSISTENCY).collect();
    let mask_y: Vec<bool> = gy.iter().map(|&g| count > 0 && g.abs() >= CONSISTENCY).collect();
    let t = Template { h, w, gx, gy, mask_x, mask_y };
    let strength = strength(&t);
    (t, strength)
}

/// 隅ごとの強さ: マスクの |g| の平均 (マスクが無ければ 0)
pub(crate) fn strength(t: &Template) -> [f32; 4] {
    let mut strength = [0f32; 4];
    let (xl, yl) = (t.x_len(), t.y_len());
    for (c, s) in strength.iter_mut().enumerate() {
        let mut selected: Vec<f32> = Vec::new();
        for k in c * xl..(c + 1) * xl {
            if t.mask_x[k] {
                selected.push(t.gx[k].abs());
            }
        }
        for k in c * yl..(c + 1) * yl {
            if t.mask_y[k] {
                selected.push(t.gy[k].abs());
            }
        }
        if !selected.is_empty() {
            // numpy の mean (float32 のペアワイズ和 / 個数)
            *s = pairwise_sum_f32(&selected) / selected.len() as f32;
        }
    }
    strength
}

/// 型の値 (gx・gy は float32、マスクは uint8 のバイト列、強さ 4 つ)
pub(crate) type TemplateTuple<'py> = (Bound<'py, PyBytes>, Bound<'py, PyBytes>, Bound<'py, PyBytes>, Bound<'py, PyBytes>, Vec<f32>);

pub(crate) fn template_tuple<'py>(py: Python<'py>, t: &Template, strength: [f32; 4]) -> TemplateTuple<'py> {
    (to_bytes(py, &t.gx, |v: f32| v.to_le_bytes().to_vec()), to_bytes(py, &t.gy, |v: f32| v.to_le_bytes().to_vec()),
     PyBytes::new(py, &t.mask_x.iter().map(|&m| m as u8).collect::<Vec<u8>>()),
     PyBytes::new(py, &t.mask_y.iter().map(|&m| m as u8).collect::<Vec<u8>>()), strength.to_vec())
}

/// 縁の向きの合計 (int64、sum_x (4, h, w-1)・sum_y (4, h-1, w)) と数えたフレーム数から型
#[pyfunction]
pub fn logo_template<'py>(py: Python<'py>, sum_x: &[u8], sum_y: &[u8], h: usize, w: usize, count: u64)
                          -> TemplateTuple<'py> {
    let n = count.max(1) as f64;
    let gx: Vec<f32> = bytes_i64(sum_x).iter().map(|&s| (s as f64 / n) as f32).collect();
    let gy: Vec<f32> = bytes_i64(sum_y).iter().map(|&s| (s as f64 / n) as f32).collect();
    let (t, strength) = from_consistency(h, w, gx, gy, count);
    template_tuple(py, &t, strength)
}

/// 縮小した四隅 (uint8、k × (4, H, W)) を 1/2 にして縁の向きを数えた合計 (sum_x・sum_y は int64)
#[pyfunction]
pub fn logo_sums_from_corners<'py>(py: Python<'py>, corners: &[u8], k: usize, big_h: usize, big_w: usize)
                                   -> (Bound<'py, PyBytes>, Bound<'py, PyBytes>) {
    let (h, w) = (big_h / 2, big_w / 2);
    let mut sum_x = vec![0i64; 4 * h * w.saturating_sub(1)];
    let mut sum_y = vec![0i64; 4 * h.saturating_sub(1) * w];
    let frame_size = 4 * big_h * big_w;
    let mut small = Vec::new();
    for f in 0..k {
        downscale(&corners[f * frame_size..(f + 1) * frame_size], big_h, big_w, &mut small);
        add_signs(&small, h, w, &mut sum_x, &mut sum_y);
    }
    (to_bytes(py, &sum_x, |v: i64| v.to_le_bytes().to_vec()), to_bytes(py, &sum_y, |v: i64| v.to_le_bytes().to_vec()))
}

/// 1 つの隅の一致度の計算の準備: 横・縦の縁のマスクの位置と型の割合、割合の絶対値の合計
type CornerScorer = (Vec<(usize, f32)>, Vec<(usize, f32)>, f32);

/// 一致度の計算の準備 (ロゴの無い隅は None)
struct Scorer {
    corners: Vec<Option<CornerScorer>>,
    h: usize,
    w: usize,
}

impl Scorer {
    fn new(t: &Template) -> Scorer {
        let (xl, yl) = (t.x_len(), t.y_len());
        let corners = (0..4)
            .map(|c| {
                if !t.has_logo(c) {
                    return None;
                }
                let xs: Vec<(usize, f32)> =
                    (0..xl).filter(|&k| t.mask_x[c * xl + k]).map(|k| (k, t.gx[c * xl + k])).collect();
                let ys: Vec<(usize, f32)> =
                    (0..yl).filter(|&k| t.mask_y[c * yl + k]).map(|k| (k, t.gy[c * yl + k])).collect();
                let abs_x: Vec<f32> = xs.iter().map(|p| p.1.abs()).collect();
                let abs_y: Vec<f32> = ys.iter().map(|p| p.1.abs()).collect();
                let total = pairwise_sum_f32(&abs_x) + pairwise_sum_f32(&abs_y);
                Some((xs, ys, total))
            })
            .collect();
        Scorer { corners, h: t.h, w: t.w }
    }

    fn any(&self) -> bool {
        self.corners.iter().any(|c| c.is_some())
    }

    /// 縮小した四隅 1 フレーム (4, h, w) の一致度 (0〜255) を out に 4 つ足す
    fn score(&self, small: &[u8], out: &mut Vec<u8>) {
        let (h, w) = (self.h, self.w);
        for (c, corner) in self.corners.iter().enumerate() {
            let Some((xs, ys, total)) = corner else {
                out.push(0);
                continue;
            };
            let px = |k: usize| small[c * h * w + k] as i16;
            // 型の割合で重み付けした縁の向きの合計 (float64 で足して float32 にする)
            let mut vx = 0f64;
            for &(k, g) in xs {
                let (y, x) = (k / (w - 1), k % (w - 1));
                vx += sign(px(y * w + x + 1) - px(y * w + x)) as f64 * g as f64;
            }
            let mut vy = 0f64;
            for &(k, g) in ys {
                vy += sign(px(k + w) - px(k)) as f64 * g as f64;
            }
            let vote = vx as f32 + vy as f32;
            let r = (vote / total).clamp(0.0, 1.0);
            out.push((r * 255.0).round_ties_even() as u8);
        }
    }
}

pub(crate) fn template_from(h: usize, w: usize, gx: &[u8], gy: &[u8], mask_x: &[u8], mask_y: &[u8]) -> Template {
    Template { h, w, gx: bytes_f32(gx), gy: bytes_f32(gy), mask_x: mask_x.iter().map(|&m| m != 0).collect(),
               mask_y: mask_y.iter().map(|&m| m != 0).collect() }
}

/// 一時ファイル (uint8 の 1/8 の四隅、count × (4, h, w)) のフレームごとの一致度 (uint8、count × 4)。
/// 型 (gx・gy は float32、マスクは uint8) は h・w の大きさ
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn logo_scores_from_file<'py>(py: Python<'py>, path: &str, count: usize, h: usize, w: usize, gx: &[u8],
                                  gy: &[u8], mask_x: &[u8], mask_y: &[u8]) -> PyResult<Bound<'py, PyBytes>> {
    let template = template_from(h, w, gx, gy, mask_x, mask_y);
    let out = py.detach(|| -> std::io::Result<Vec<u8>> {
        let scorer = Scorer::new(&template);
        let mut out = Vec::with_capacity(count * 4);
        if count == 0 || !scorer.any() {
            out.resize(count * 4, 0);
            return Ok(out);
        }
        let mut reader = BufReader::with_capacity(1 << 22, File::open(path)?);
        let mut frame = vec![0u8; 4 * h * w];
        for _ in 0..count {
            reader.read_exact(&mut frame)?;
            scorer.score(&frame, &mut out);
        }
        Ok(out)
    })?;
    Ok(PyBytes::new(py, &out))
}

/// 縮小した四隅 (uint8、k × (4, H, W)) を 1/2 にした一致度 (uint8、k × 4)。型は (H/2, W/2) の大きさ
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn logo_scores_from_corners<'py>(py: Python<'py>, corners: &[u8], k: usize, big_h: usize, big_w: usize,
                                     gx: &[u8], gy: &[u8], mask_x: &[u8], mask_y: &[u8]) -> Bound<'py, PyBytes> {
    let (h, w) = (big_h / 2, big_w / 2);
    let scorer = Scorer::new(&template_from(h, w, gx, gy, mask_x, mask_y));
    let frame_size = 4 * big_h * big_w;
    let mut out = Vec::with_capacity(k * 4);
    let mut small = Vec::new();
    for f in 0..k {
        downscale(&corners[f * frame_size..(f + 1) * frame_size], big_h, big_w, &mut small);
        scorer.score(&small, &mut out);
    }
    PyBytes::new(py, &out)
}

// ---- ロゴ画像の材料 (logo_image.py) -------------------------------------------------

// ロゴの型の 1 画素が元の解像度の何画素か (特徴の 1/4 × ロゴの型の 1/2)
const TEMPLATE_SCALE: i64 = 8;
// 型のマスクの外接矩形の周りに足す余白 (元の解像度の画素)
const PAD: i64 = 8;
// 隅の高さ・幅のこの割合以上続く直線は、ロゴではなく番組の画面の枠として範囲から除く
const LINE_FRACTION: f64 = 0.75;
// 型の画素でこの数以内に離れた縁は同じ塊 (ロゴの文字の間) とみなす
const CLUSTER_GAP: usize = 2;
// 背景を測る輪の幅 (元の解像度の画素)、輪の Y の標準偏差の上限、輪の最小の画素数
const RING: i64 = 8;
const FLAT_STD: f64 = 6.0;
const MIN_RING_PIXELS: usize = 64;

/// 型のマスクの縁に接する画素 (型の解像度、gh × gw)
fn edge_pixels(t: &Template, c: usize) -> (Vec<bool>, usize, usize) {
    let (h, w) = (t.h, t.w);
    let (xw, yh) = (w.saturating_sub(1), h.saturating_sub(1));
    let gh = h.max(yh + 1);
    let gw = (xw + 1).max(w);
    let mut grid = vec![false; gh * gw];
    let (xl, yl) = (t.x_len(), t.y_len());
    for k in 0..xl {
        if t.mask_x[c * xl + k] {
            let (y, x) = (k / xw, k % xw); // 横の縁: 画素 x と x+1 の間
            grid[y * gw + x] = true;
            grid[y * gw + x + 1] = true;
        }
    }
    for k in 0..yl {
        if t.mask_y[c * yl + k] {
            let (y, x) = (k / w, k % w); // 縦の縁: 画素 y と y+1 の間
            grid[y * gw + x] = true;
            grid[(y + 1) * gw + x] = true;
        }
    }
    (grid, gh, gw)
}

/// 縦・横に高さ・幅の LINE_FRACTION 以上続く線を除く (見つけるのは元の grid、消すのは写し)
fn without_long_lines(grid: &[bool], gh: usize, gw: usize) -> Vec<bool> {
    let mut out = grid.to_vec();
    for x in 0..gw {
        let mut y = 0;
        while y < gh {
            if !grid[y * gw + x] {
                y += 1;
                continue;
            }
            let s = y;
            while y < gh && grid[y * gw + x] {
                y += 1;
            }
            if (y - s) as f64 >= LINE_FRACTION * gh as f64 {
                for yy in s..y {
                    out[yy * gw + x] = false;
                }
            }
        }
    }
    for y in 0..gh {
        let mut x = 0;
        while x < gw {
            if !grid[y * gw + x] {
                x += 1;
                continue;
            }
            let s = x;
            while x < gw && grid[y * gw + x] {
                x += 1;
            }
            if (x - s) as f64 >= LINE_FRACTION * gw as f64 {
                for xx in s..x {
                    out[y * gw + xx] = false;
                }
            }
        }
    }
    out
}

/// CLUSTER_GAP 画素以内の画素をつなげた塊のうち、画素の一番多い塊
fn largest_cluster(grid: &[bool], gh: usize, gw: usize) -> Vec<bool> {
    let mut near = grid.to_vec();
    for _ in 0..CLUSTER_GAP {
        let mut grown = near.clone();
        for y in 0..gh {
            for x in 0..gw {
                let i = y * gw + x;
                if (y > 0 && near[i - gw]) || (y + 1 < gh && near[i + gw]) || (x > 0 && near[i - 1])
                    || (x + 1 < gw && near[i + 1])
                {
                    grown[i] = true;
                }
            }
        }
        near = grown;
    }
    let mut labels = vec![0u32; gh * gw];
    let (mut best, mut best_count, mut label) = (0u32, 0usize, 0u32);
    for start in 0..gh * gw {
        if !near[start] || labels[start] != 0 {
            continue;
        }
        label += 1;
        labels[start] = label;
        let mut stack = vec![start];
        while let Some(i) = stack.pop() {
            let (cy, cx) = (i / gw, i % gw);
            let mut visit = |ny: usize, nx: usize| {
                let j = ny * gw + nx;
                if near[j] && labels[j] == 0 {
                    labels[j] = label;
                    stack.push(j);
                }
            };
            if cy > 0 {
                visit(cy - 1, cx);
            }
            if cy + 1 < gh {
                visit(cy + 1, cx);
            }
            if cx > 0 {
                visit(cy, cx - 1);
            }
            if cx + 1 < gw {
                visit(cy, cx + 1);
            }
        }
        let count = (0..gh * gw).filter(|&i| grid[i] && labels[i] == label).count();
        if count > best_count {
            best = label;
            best_count = count;
        }
    }
    (0..gh * gw).map(|i| best != 0 && grid[i] && labels[i] == best).collect()
}

/// 型のマスクから、ロゴのある隅ごとの範囲 (隅, x0, y0, x1, y1)。隅の中の座標、元の解像度、余白込み、偶数にそろえる
fn boxes(t: &Template, width: i64, height: i64) -> Vec<(usize, i64, i64, i64, i64)> {
    let mut result = Vec::new();
    for c in 0..4 {
        if !t.has_logo(c) {
            continue;
        }
        let (grid, gh, gw) = edge_pixels(t, c);
        let cluster = largest_cluster(&without_long_lines(&grid, gh, gw), gh, gw);
        let cells: Vec<(i64, i64)> =
            (0..gh * gw).filter(|&i| cluster[i]).map(|i| ((i / gw) as i64, (i % gw) as i64)).collect();
        if cells.is_empty() {
            continue;
        }
        let (rmin, rmax) = (cells.iter().map(|p| p.0).min().unwrap(), cells.iter().map(|p| p.0).max().unwrap());
        let (cmin, cmax) = (cells.iter().map(|p| p.1).min().unwrap(), cells.iter().map(|p| p.1).max().unwrap());
        let x0 = (cmin * TEMPLATE_SCALE - PAD).max(0) / 2 * 2;
        let y0 = (rmin * TEMPLATE_SCALE - PAD).max(0) / 2 * 2;
        let x1 = ((cmax + 1) * TEMPLATE_SCALE + PAD).min(width);
        let y1 = ((rmax + 1) * TEMPLATE_SCALE + PAD).min(height);
        let (x1, y1) = ((x1 + 1) / 2 * 2, (y1 + 1) / 2 * 2);
        if x1 > x0 && y1 > y0 {
            result.push((c, x0, y0, x1, y1));
        }
    }
    result
}

/// 1 つの隅の合計 (box はフレームの座標 (x, y, 幅, 高さ)。sum_v・sum_bv は面ごと (Cb・Cr は縦横 1/2))
type CornerSamples = (usize, (i64, i64, i64, i64), u64, Vec<f64>, Vec<f64>, Vec<Vec<f64>>, Vec<Vec<f64>>);

/// 1 つの隅の合計を、モザイク (四隅を 2×2 に並べた yuv420p、2W × 2H) を 1 枚ずつ受け取って作る
struct CornerAccumulator {
    corner: usize,
    bounds: (i64, i64, i64, i64), // 隅の中の範囲 (x0, y0, x1, y1)
    mosaic: (i64, i64),           // モザイクの中の隅の左上 (mx, my)
    w2: i64,
    y_size: i64,
    c_size: i64,
    ring: Vec<(i64, i64)>,   // 輪 (背景) の画素 (隅の中の座標)
    ring_c: Vec<(i64, i64)>, // 輪のうち Cb・Cr の画素に当たる所 (偶数の座標)
    n: u64,
    sum_b: Vec<f64>,
    sum_bb: Vec<f64>,
    sum_v: Vec<Vec<f64>>,
    sum_bv: Vec<Vec<f64>>,
}

impl CornerAccumulator {
    /// 輪が MIN_RING_PIXELS に足りない (ロゴが隅の端に接して輪が取れない) 時は None
    fn new(width: i64, height: i64, corner: usize, b: (i64, i64, i64, i64)) -> Option<CornerAccumulator> {
        let (x0, y0, x1, y1) = b;
        // 輪: 範囲を RING 広げた矩形 (隅の中に収める) から範囲を除いたもの
        let (ox0, oy0) = ((x0 - RING).max(0) / 2 * 2, (y0 - RING).max(0) / 2 * 2);
        let (ox1, oy1) = ((x1 + RING).min(width) / 2 * 2, (y1 + RING).min(height) / 2 * 2);
        let inside = move |x: i64, y: i64| x0 <= x && x < x1 && y0 <= y && y < y1;
        let ring: Vec<(i64, i64)> =
            (oy0..oy1).flat_map(|y| (ox0..ox1).map(move |x| (x, y))).filter(|&(x, y)| !inside(x, y)).collect();
        let ring_c: Vec<(i64, i64)> = (0..(oy1 - oy0 + 1) / 2)
            .flat_map(|yy| (0..(ox1 - ox0 + 1) / 2).map(move |xx| (ox0 + 2 * xx, oy0 + 2 * yy)))
            .filter(|&(x, y)| !inside(x, y))
            .collect();
        if ring.len() < MIN_RING_PIXELS {
            return None;
        }
        let (w2, h2) = (2 * width, 2 * height);
        let (h, w) = (y1 - y0, x1 - x0);
        let sizes = [(h * w) as usize, (h / 2 * (w / 2)) as usize, (h / 2 * (w / 2)) as usize];
        Some(CornerAccumulator {
            corner,
            bounds: b,
            mosaic: ((corner as i64 % 2) * width, (corner as i64 / 2) * height),
            w2,
            y_size: w2 * h2,
            c_size: (w2 / 2) * (h2 / 2),
            ring,
            ring_c,
            n: 0,
            sum_b: vec![0.0; 3],
            sum_bb: vec![0.0; 3],
            sum_v: sizes.iter().map(|&s| vec![0.0; s]).collect(),
            sum_bv: sizes.iter().map(|&s| vec![0.0; s]).collect(),
        })
    }

    fn add(&mut self, frame: &[u8]) {
        let (x0, y0, x1, y1) = self.bounds;
        let (mx, my) = self.mosaic;
        let (w2, y_size, c_size) = (self.w2, self.y_size, self.c_size);
        let yv = |x: i64, y: i64| frame[((my + y) * w2 + mx + x) as usize] as f64;
        let cv = |p: usize, x: i64, y: i64| {
            let base = y_size + if p == 1 { 0 } else { c_size };
            frame[(base + ((my + y) / 2) * (w2 / 2) + (mx + x) / 2) as usize] as f64
        };
        // 輪の Y の標準偏差 (背景が平らなフレームだけ使う)
        let count = self.ring.len() as f64;
        let mean = self.ring.iter().map(|&(x, y)| yv(x, y)).sum::<f64>() / count;
        let var = self.ring.iter().map(|&(x, y)| (yv(x, y) - mean) * (yv(x, y) - mean)).sum::<f64>() / count;
        if var.sqrt() > FLAT_STD {
            return;
        }
        self.n += 1;
        let (h, w) = (y1 - y0, x1 - x0);
        for p in 0..3 {
            let bg = if p == 0 {
                mean
            } else {
                self.ring_c.iter().map(|&(x, y)| cv(p, x, y)).sum::<f64>() / self.ring_c.len() as f64
            };
            self.sum_b[p] += bg;
            self.sum_bb[p] += bg * bg;
            let (ph, pw) = if p == 0 { (h, w) } else { (h / 2, w / 2) };
            for yy in 0..ph {
                for xx in 0..pw {
                    let v = if p == 0 { yv(x0 + xx, y0 + yy) } else { cv(p, x0 + 2 * xx, y0 + 2 * yy) };
                    let k = (yy * pw + xx) as usize;
                    self.sum_v[p][k] += v;
                    self.sum_bv[p][k] += bg * v;
                }
            }
        }
    }

    /// 平らな背景のフレームが無ければ None
    fn finish(self, frame_w: i64, frame_h: i64, width: i64, height: i64) -> Option<CornerSamples> {
        if self.n == 0 {
            return None;
        }
        let (x0, y0, x1, y1) = self.bounds;
        let c = self.corner;
        let (ox, oy) = (if c.is_multiple_of(2) { 0 } else { frame_w - width }, if c < 2 { 0 } else { frame_h - height });
        Some((c, (ox + x0, oy + y0, x1 - x0, y1 - y0), self.n, self.sum_b, self.sum_bb, self.sum_v, self.sum_bv))
    }
}

/// 1 つの隅の合計 (隅, 範囲, フレーム数, sum_b, sum_bb, sum_v (面ごとの float64 のバイト列), sum_bv)
type SamplesTuple<'py> = (usize, (i64, i64, i64, i64), u64, Vec<f64>, Vec<f64>, Vec<Bound<'py, PyBytes>>,
                          Vec<Bound<'py, PyBytes>>);

/// ロゴ画像の材料: 範囲 (隅, x0, y0, x1, y1。logo_boxes) ごとに、一時ファイル (モザイク count 枚) の平らな背景の
/// フレームの合計。元の解像度の四隅は width × height、フレームは frame_w × frame_h。一時ファイルは 1 回だけ読む
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn logo_image_samples<'py>(py: Python<'py>, spool: &str, count: usize, frame_w: i64, frame_h: i64, width: i64,
                               height: i64, boxes: Vec<(usize, i64, i64, i64, i64)>)
                               -> PyResult<Vec<SamplesTuple<'py>>> {
    let mut accumulators: Vec<CornerAccumulator> = boxes
        .into_iter()
        .filter_map(|(c, x0, y0, x1, y1)| CornerAccumulator::new(width, height, c, (x0, y0, x1, y1)))
        .collect();
    let results = py.detach(|| -> std::io::Result<Vec<CornerSamples>> {
        if count == 0 || accumulators.is_empty() {
            return Ok(Vec::new());
        }
        let mut reader = BufReader::with_capacity(1 << 22, File::open(spool)?);
        let mut frame = vec![0u8; (2 * width * 2 * height * 3 / 2) as usize];
        for _ in 0..count {
            reader.read_exact(&mut frame)?;
            for a in accumulators.iter_mut() {
                a.add(&frame);
            }
        }
        Ok(accumulators.into_iter().filter_map(|a| a.finish(frame_w, frame_h, width, height)).collect())
    })?;
    Ok(results
        .into_iter()
        .map(|(c, b, n, sb, sbb, sv, sbv)| {
            let planes =
                |v: Vec<Vec<f64>>| v.iter().map(|p| to_bytes(py, p, |x: f64| x.to_le_bytes().to_vec())).collect();
            (c, b, n, sb, sbb, planes(sv), planes(sbv))
        })
        .collect())
}

/// ロゴのある隅ごとの範囲 (隅, x0, y0, x1, y1)。確かめ用 (logo_image.logo_boxes)
#[pyfunction]
pub fn logo_boxes(h: usize, w: usize, mask_x: &[u8], mask_y: &[u8], width: i64, height: i64)
                  -> Vec<(usize, i64, i64, i64, i64)> {
    let t = Template { h, w, gx: Vec::new(), gy: Vec::new(), mask_x: mask_x.iter().map(|&m| m != 0).collect(),
                       mask_y: mask_y.iter().map(|&m| m != 0).collect() };
    boxes(&t, width, height)
}

/// コマごとの一致度 (uint8、count × 4) を、フレームごとのコマの番号 (int64 のバイト列) で並べ替えて、
/// 隅ごとのフレームの一致度 (uint8、4 × フレーム数) にする (numpy の scores[idx].T。2026-10-30 までは numpy)。
/// 範囲外のコマの番号は 0 にする
#[pyfunction]
pub fn frame_scores<'py>(py: Python<'py>, scores: &[u8], count: usize, idx: &[u8]) -> Bound<'py, PyBytes> {
    let idx = bytes_i64(idx);
    let n = idx.len();
    let mut out = vec![0u8; 4 * n];
    for (f, &i) in idx.iter().enumerate() {
        if i >= 0 && (i as usize) < count && (i as usize) * 4 + 3 < scores.len() {
            for c in 0..4 {
                out[c * n + f] = scores[i as usize * 4 + c];
            }
        }
    }
    PyBytes::new(py, &out)
}
