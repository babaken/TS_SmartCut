//! ロゴの型どうしの計算と .lgd (設計書 §31.9、段階 ③-b。2026-10-30 までは Python の numpy)。
//!
//! - 局の型のまとめ方 (logo.py の combine_templates): 録画ごとの割合をフレーム数の重みで平均し、使った録画の
//!   2/3 以上でマスクだった画素だけを残す
//! - 型どうしの比べ方 (logo.py の logo_agreement、logo_change.py の two_way_agreement)
//! - 取り込んだ .lgd の型 (lgd_import.py の template_from_lgd): いろいろな明るさの背景にロゴを重ねた隅を
//!   特徴の計算と同じ縮小で作り、縁の向きを数える
//! - ロゴ画像の材料からロゴを求める (logo_image.py の solve。.lgd の書き出しに使う)
//! - 書き出す .lgd のバイト列 (lgd_export.py): エンコードソフトが読む形 (標準の ver0.1 + 独自の拡張)
//!
//! まとめ方・比べ方・solve は numpy の時とビットまで同じ (足す順も同じ)。.lgd の型だけは背景のノイズの乱数が
//! numpy と違うので、型の値は少し変わる (取り込み済みの型は DB に残っていて作り直さない)。

use crate::post::{
    CONSISTENCY, Template, TemplateTuple, add_signs, bytes_f32, bytes_f64, downscale, from_consistency, strength,
    template_from, template_tuple, to_bytes,
};
use pyo3::prelude::*;
use pyo3::types::PyBytes;

// 局の型のマスクは、使った録画のこの割合以上で (録画ごとの型の) マスクだった画素だけ (logo.py の STATION_AGREEMENT)
const STATION_AGREEMENT: f64 = 2.0 / 3.0;
// .lgd の型: 背景の数、ノイズ (8 ビットの標準偏差)、特徴の計算の縮小 (features.py の SCALE_DIVISOR・CORNER_DIVISOR)
const BACKGROUNDS: usize = 64;
const BACKGROUND_NOISE: f64 = 3.0;
const SCALE: usize = 4;
const CORNER: usize = 4;

// ---- 局の型のまとめ方 ---------------------------------------------------------

/// 録画 1 本の型 (gx、gy、フレーム数)
type Part = (Vec<f32>, Vec<f32>, u64);

/// 録画ごとの型 (gx・gy は float32 のバイト列、フレーム数) を局の型にまとめる。型はどれも (h, w) の大きさ
/// (呼ぶ前に logo.py の usable_for_station でそろえる)
#[pyfunction]
pub fn logo_combine<'py>(py: Python<'py>, h: usize, w: usize, templates: Vec<(Vec<u8>, Vec<u8>, u64)>)
                         -> TemplateTuple<'py> {
    let parts: Vec<Part> =
        templates.iter().map(|(gx, gy, frames)| (bytes_f32(gx), bytes_f32(gy), *frames)).collect();
    let frames: u64 = parts.iter().map(|p| p.2).sum();
    // numpy: sum(t.gx.astype(float64) * t.frames for t in usable) / frames (録画の順に足す)
    let weighted = |pick: fn(&Part) -> &Vec<f32>| -> Vec<f32> {
        let len = parts.first().map_or(0, |p| pick(p).len());
        let mut acc = vec![0f64; len];
        for p in &parts {
            for (a, &g) in acc.iter_mut().zip(pick(p)) {
                *a += g as f64 * p.2 as f64;
            }
        }
        acc.iter().map(|&a| (a / frames as f64) as f32).collect()
    };
    let gx = weighted(|p| &p.0);
    let gy = weighted(|p| &p.1);
    let (mut t, _) = from_consistency(h, w, gx, gy, frames);
    let needed = (parts.len() as f64 * STATION_AGREEMENT - 1e-9).ceil() as usize;
    let agree = |pick: fn(&Part) -> &Vec<f32>, k: usize| {
        parts.iter().filter(|p| pick(p)[k].abs() >= CONSISTENCY).count() >= needed
    };
    for k in 0..t.mask_x.len() {
        t.mask_x[k] = t.mask_x[k] && agree(|p| &p.0, k);
    }
    for k in 0..t.mask_y.len() {
        t.mask_y[k] = t.mask_y[k] && agree(|p| &p.1, k);
    }
    let s = strength(&t);
    template_tuple(py, &t, s)
}

// ---- 型どうしの比べ方 ---------------------------------------------------------

/// numpy の np.sign (0 は 0)
fn sign(g: f32) -> f32 {
    if g > 0.0 {
        1.0
    } else if g < 0.0 {
        -1.0
    } else {
        0.0
    }
}

/// 型 a のマスクの縁で、型 b と縁の向きが合う割合 (0〜1)。b にロゴが無い・a にロゴが無い時は None。
/// b のロゴが別の隅にしか無ければ、その隅の縁は合わない (0) として数える
fn agreement(a: &Template, b: &Template) -> Option<f64> {
    if !(0..4).any(|c| b.has_logo(c)) {
        return None;
    }
    let (xl, yl) = (a.x_len(), a.y_len());
    let (mut total, mut agreed) = (0usize, 0usize);
    for c in 0..4 {
        if !a.has_logo(c) {
            continue;
        }
        let other = b.has_logo(c);
        let mut vote = |mask: &[bool], ga: &[f32], gb: &[f32]| {
            for k in 0..mask.len() {
                if mask[k] {
                    total += 1;
                    // numpy: np.sign(a) * b (float32) >= 0.3 (float32 で比べる)
                    if other && sign(ga[k]) * gb[k] >= CONSISTENCY {
                        agreed += 1;
                    }
                }
            }
        };
        vote(&a.mask_x[c * xl..(c + 1) * xl], &a.gx[c * xl..(c + 1) * xl], &b.gx[c * xl..(c + 1) * xl]);
        vote(&a.mask_y[c * yl..(c + 1) * yl], &a.gy[c * yl..(c + 1) * yl], &b.gy[c * yl..(c + 1) * yl]);
    }
    if total == 0 { None } else { Some(agreed as f64 / total as f64) }
}

type TemplateArrays = (Vec<u8>, Vec<u8>, Vec<u8>, Vec<u8>);

fn to_template(h: usize, w: usize, t: &TemplateArrays) -> Template {
    template_from(h, w, &t.0, &t.1, &t.2, &t.3)
}

/// 型 a (gx・gy は float32、マスクは uint8 のバイト列) のマスクの縁で、型 b と縁の向きが合う割合。
/// 大きさが違う時は None (logo.py の logo_agreement)
#[pyfunction]
pub fn logo_agreement(ha: usize, wa: usize, a: TemplateArrays, hb: usize, wb: usize, b: TemplateArrays) -> Option<f64> {
    if (ha, wa) != (hb, wb) {
        return None;
    }
    agreement(&to_template(ha, wa, &a), &to_template(hb, wb, &b))
}

/// 2 つの型の合い方: どちらから見た一致の大きい方 (どちらも測れなければ None。logo_change.py の two_way_agreement)
#[pyfunction]
pub fn logo_two_way_agreement(ha: usize, wa: usize, a: TemplateArrays, hb: usize, wb: usize, b: TemplateArrays)
                              -> Option<f64> {
    if (ha, wa) != (hb, wb) {
        return None;
    }
    let (ta, tb) = (to_template(ha, wa, &a), to_template(hb, wb, &b));
    match (agreement(&ta, &tb), agreement(&tb, &ta)) {
        (Some(x), Some(y)) => Some(x.max(y)),
        (x, y) => x.or(y),
    }
}

// ---- 取り込んだ .lgd の型 -------------------------------------------------------

/// 背景のノイズの乱数 (SplitMix64 の一様乱数から Box-Muller で正規分布。種は固定)
struct Noise {
    state: u64,
    spare: Option<f64>,
}

impl Noise {
    fn new(seed: u64) -> Noise {
        Noise { state: seed, spare: None }
    }

    fn uniform(&mut self) -> f64 {
        self.state = self.state.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^= z >> 31;
        // (0, 1] (対数を取るので 0 を除く)
        ((z >> 11) as f64 + 1.0) / (1u64 << 53) as f64
    }

    fn normal(&mut self) -> f64 {
        if let Some(v) = self.spare.take() {
            return v;
        }
        let (u1, u2) = (self.uniform(), self.uniform());
        let r = (-2.0 * u1.ln()).sqrt();
        let theta = 2.0 * std::f64::consts::PI * u2;
        self.spare = Some(r * theta.sin());
        r * theta.cos()
    }
}

/// .lgd のロゴ 1 つ (不透明度 0〜1・輝度 8 ビットは float32 のバイト列、(高さ, 幅)) を、その映像の大きさの録画の
/// 局の型にする: (型の高さ, 幅, 型)。ロゴが映像の外にはみ出す・どの隅にも縁が残らない時は None
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn lgd_template<'py>(py: Python<'py>, alpha: &[u8], luma: &[u8], x: i64, y: i64, width: i64, height: i64,
                         image_width: i64, image_height: i64) -> Option<(usize, usize, TemplateTuple<'py>)> {
    if image_width <= 0 || image_height <= 0 || width <= 0 || height <= 0 || x < 0 || y < 0
        || x + width > image_width || y + height > image_height
    {
        return None;
    }
    let (alpha, luma) = (bytes_f32(alpha), bytes_f32(luma));
    let (lw, lh) = (width as usize, height as usize);
    if alpha.len() != lw * lh || luma.len() != lw * lh {
        return None;
    }
    let (lx, ly) = (x as usize, y as usize);
    // 特徴の計算と同じ大きさ: 1/4 にした映像 (w, h)、その 1/4 の四隅 (cw, ch)
    let (w, h) = ((image_width as usize).div_ceil(SCALE), (image_height as usize).div_ceil(SCALE));
    let (cw, ch) = (w / CORNER, h / CORNER);
    let (full_w, full_h) = (cw * SCALE, ch * SCALE);
    let origins = [(0, 0), (0, (w - cw) * SCALE), ((h - ch) * SCALE, 0), ((h - ch) * SCALE, (w - cw) * SCALE)];

    let result = py.detach(|| {
        let mut noise = Noise::new(0);
        let (small_h, small_w) = (ch / 2, cw / 2);
        let mut sum_x = vec![0i64; 4 * small_h * small_w.saturating_sub(1)];
        let mut sum_y = vec![0i64; 4 * small_h.saturating_sub(1) * small_w];
        let mut corners = vec![0u8; 4 * ch * cw];
        let mut image = vec![0f32; full_h * full_w];
        let mut small = Vec::new();
        for b in 0..BACKGROUNDS {
            // 背景の明るさは 16〜235 を均等に (numpy の linspace を float32 にしたもの)
            let level = (16.0 + b as f64 * (219.0 / (BACKGROUNDS - 1) as f64)) as f32;
            for (c, &(oy, ox)) in origins.iter().enumerate() {
                let corner = &mut corners[c * ch * cw..(c + 1) * ch * cw];
                let (y0, y1) = (ly.max(oy), (ly + lh).min(oy + full_h));
                let (x0, x1) = (lx.max(ox), (lx + lw).min(ox + full_w));
                if y0 >= y1 || x0 >= x1 {
                    // ロゴの重ならない隅は平らな背景のまま (縁が無いので数えない)
                    corner.fill(level as u8);
                    continue;
                }
                for v in image.iter_mut() {
                    *v = (level as f64 + noise.normal() * BACKGROUND_NOISE) as f32;
                }
                // ロゴの重なる部分: 背景 × (1 − α) + ロゴの色 × α
                for yy in y0..y1 {
                    for xx in x0..x1 {
                        let k = (yy - ly) * lw + (xx - lx);
                        let (a, c) = (alpha[k], luma[k]);
                        let v = &mut image[(yy - oy) * full_w + (xx - ox)];
                        *v = *v * (1.0 - a) + c * a;
                    }
                }
                // 1/4 の縮小 (ffmpeg の area と同じ 4×4 の平均) と 8 ビットへの丸め
                for sy in 0..ch {
                    for sx in 0..cw {
                        let mut total = 0f64;
                        for dy in 0..SCALE {
                            for dx in 0..SCALE {
                                total += image[(sy * SCALE + dy) * full_w + sx * SCALE + dx] as f64;
                            }
                        }
                        let mean = total / (SCALE * SCALE) as f64;
                        corner[sy * cw + sx] = mean.round_ties_even().clamp(0.0, 255.0) as u8;
                    }
                }
            }
            downscale(&corners, ch, cw, &mut small);
            add_signs(&small, small_h, small_w, &mut sum_x, &mut sum_y);
        }
        let n = BACKGROUNDS as f64;
        let gx: Vec<f32> = sum_x.iter().map(|&s| (s as f64 / n) as f32).collect();
        let gy: Vec<f32> = sum_y.iter().map(|&s| (s as f64 / n) as f32).collect();
        let (t, s) = from_consistency(small_h, small_w, gx, gy, BACKGROUNDS as u64);
        if (0..4).any(|c| t.has_logo(c)) { Some((t, s)) } else { None }
    });
    result.map(|(t, s)| (t.h, t.w, template_tuple(py, &t, s)))
}

// ---- ロゴ画像の材料からロゴを求める ---------------------------------------------------

/// 1 つの隅の合計 (範囲 (x, y, 幅, 高さ)、フレーム数、sum_b、sum_bb (面ごと)、sum_v・sum_bv (面ごとの float64 のバイト列))
type SolveInput = ((i64, i64, i64, i64), u64, Vec<f64>, Vec<f64>, Vec<Vec<u8>>, Vec<Vec<u8>>);

/// 録画 1 本の面ごとの sum_v・sum_bv
type PlaneSums = (Vec<Vec<f64>>, Vec<Vec<f64>>);

/// 範囲、面ごとの不透明度・ロゴの色・使ったフレーム数 (float64 のバイト列。Y (h, w)、Cb・Cr (h/2, w/2))
type Solved<'py> = ((i64, i64, i64, i64), Vec<Bound<'py, PyBytes>>, Vec<Bound<'py, PyBytes>>, Vec<Bound<'py, PyBytes>>);

/// 1 つの隅の合計 (複数の録画をまとめてよい) から不透明度と色を求める。範囲が録画ごとに違う時は合わせた範囲で、
/// 画素ごとにその画素を含む録画の合計を足す。合計が無ければ None
#[pyfunction]
pub fn logo_solve<'py>(py: Python<'py>, samples: Vec<SolveInput>) -> Option<Solved<'py>> {
    if samples.is_empty() {
        return None;
    }
    let x0 = samples.iter().map(|s| s.0.0).min()?;
    let y0 = samples.iter().map(|s| s.0.1).min()?;
    let x1 = samples.iter().map(|s| s.0.0 + s.0.2).max()?;
    let y1 = samples.iter().map(|s| s.0.1 + s.0.3).max()?;
    let sums: Vec<PlaneSums> = samples
        .iter()
        .map(|s| (s.4.iter().map(|v| bytes_f64(v)).collect(), s.5.iter().map(|v| bytes_f64(v)).collect()))
        .collect();
    let (mut alpha, mut color, mut frames) = (Vec::new(), Vec::new(), Vec::new());
    for p in 0..3 {
        let d = if p == 0 { 1 } else { 2 };
        let (ph, pw) = (((y1 - y0) / d) as usize, ((x1 - x0) / d) as usize);
        let len = ph * pw;
        let (mut n, mut sb, mut sbb, mut sv, mut sbv) =
            (vec![0f64; len], vec![0f64; len], vec![0f64; len], vec![0f64; len], vec![0f64; len]);
        for (s, (sum_v, sum_bv)) in samples.iter().zip(&sums) {
            let (x, y, w, h) = s.0;
            let (ry, rx) = (((y - y0) / d) as usize, ((x - x0) / d) as usize);
            let (rh, rw) = (((y - y0 + h) / d) as usize - ry, ((x - x0 + w) / d) as usize - rx);
            for yy in 0..rh {
                for xx in 0..rw {
                    let (k, j) = ((ry + yy) * pw + rx + xx, yy * rw + xx);
                    n[k] += s.1 as f64;
                    sb[k] += s.2[p];
                    sbb[k] += s.3[p];
                    sv[k] += sum_v[p][j];
                    sbv[k] += sum_bv[p][j];
                }
            }
        }
        let (mut a, mut c) = (vec![0f64; len], vec![0f64; len]);
        for k in 0..len {
            (a[k], c[k]) = solve_pixel(n[k], sb[k], sbb[k], sv[k], sbv[k]);
        }
        let bytes = |v: &Vec<f64>| to_bytes(py, v, |x: f64| x.to_le_bytes().to_vec());
        alpha.push(bytes(&a));
        color.push(bytes(&c));
        frames.push(bytes(&n));
    }
    Some(((x0, y0, x1 - x0, y1 - y0), alpha, color, frames))
}

/// 1 画素の不透明度とロゴの色: 見える値 = (1 − α) × 背景 + α × 色 を、フレームの背景の明るさに当てはめた直線から
fn solve_pixel(n: f64, sb: f64, sbb: f64, sv: f64, sbv: f64) -> (f64, f64) {
    let den = n * sbb - sb * sb;
    let slope = if den > 0.0 { (n * sbv - sb * sv) / den } else { 1.0 };
    let intercept = if n > 0.0 { (sv - slope * sb) / n } else { 0.0 };
    let a = (1.0 - slope).clamp(0.0, 1.0);
    let value = if a > 0.02 { intercept / a.max(1e-9) } else { 0.0 };
    (a, value.clamp(0.0, 255.0))
}

// 録画ごとの材料を足してよいか (logo_sample_checks の値)
const SAMPLE_OK: u8 = 0;
const SAMPLE_FEW_FRAMES: u8 = 1;
const SAMPLE_DARK_OPAQUE: u8 = 2;
const SAMPLE_DISAGREES: u8 = 3;

/// 相関 (両方に値のある画素だけ)。画素が 3 未満・どちらかが一定なら −1 (合わないとみなす)
fn correlation(a: &[f64], b: &[f64]) -> f64 {
    let pairs: Vec<(f64, f64)> =
        a.iter().zip(b).filter(|(x, y)| !x.is_nan() && !y.is_nan()).map(|(x, y)| (*x, *y)).collect();
    if pairs.len() < 3 {
        return -1.0;
    }
    let n = pairs.len() as f64;
    let (ma, mb) = (pairs.iter().map(|p| p.0).sum::<f64>() / n, pairs.iter().map(|p| p.1).sum::<f64>() / n);
    let (mut sab, mut saa, mut sbb) = (0.0, 0.0, 0.0);
    for (x, y) in &pairs {
        sab += (x - ma) * (y - mb);
        saa += (x - ma) * (x - ma);
        sbb += (y - mb) * (y - mb);
    }
    if saa <= 0.0 || sbb <= 0.0 {
        return -1.0;
    }
    sab / (saa * sbb).sqrt()
}

/// 録画ごとの材料 (logo_solve と同じ形) を、局の範囲 area (フレームの座標 (x, y, 幅, 高さ)) の Y で 1 本ずつ求めて比べ、
/// 局のロゴに足してよいかを録画ごとに返す: 0 足す、1 フレームが min_frames より少ない (比べられない)、2 ほぼ不透明で
/// 暗い画素 (不透明度 dark_alpha 以上・輝度 dark_luma 未満) がロゴの画素 (不透明度 logo_alpha 超) の
/// max_dark_fraction 以上 (帯・暗い背景)、3 ほかの録画と合わない (残りが 3 本以上なら画素ごとの中央値と、2 本なら
/// 互いの、不透明度の相関が min_correlation 未満)。
/// 録画の型が番組の枠などを拾って範囲が広くなった録画は、背景を測る輪がロゴから離れ、足すと局のロゴがずれた
/// (2026-10-30 の確かめ、2 局)
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn logo_sample_checks(samples: Vec<SolveInput>, area: (i64, i64, i64, i64), min_frames: u64, logo_alpha: f64,
                          dark_alpha: f64, dark_luma: f64, max_dark_fraction: f64, min_correlation: f64) -> Vec<u8> {
    let (ax, ay, aw, ah) = area;
    let mut checks = vec![SAMPLE_OK; samples.len()];
    let mut alphas: Vec<Vec<f64>> = Vec::new();
    let mut judged: Vec<usize> = Vec::new();
    for (i, s) in samples.iter().enumerate() {
        if s.1 < min_frames {
            checks[i] = SAMPLE_FEW_FRAMES;
            continue;
        }
        let (sx, sy, sw, sh) = s.0;
        let (sum_v, sum_bv) = (bytes_f64(&s.4[0]), bytes_f64(&s.5[0]));
        let mut alpha = vec![f64::NAN; (aw * ah) as usize];
        let (mut logo, mut dark) = (0usize, 0usize);
        for y in 0..ah {
            for x in 0..aw {
                let (fx, fy) = (ax + x, ay + y);
                if fx < sx || fy < sy || fx >= sx + sw || fy >= sy + sh {
                    continue;
                }
                let j = ((fy - sy) * sw + fx - sx) as usize;
                let (a, c) = solve_pixel(s.1 as f64, s.2[0], s.3[0], sum_v[j], sum_bv[j]);
                alpha[(y * aw + x) as usize] = a;
                if a > logo_alpha {
                    logo += 1;
                    if a >= dark_alpha && c < dark_luma {
                        dark += 1;
                    }
                }
            }
        }
        if logo > 0 && dark as f64 >= max_dark_fraction * logo as f64 {
            checks[i] = SAMPLE_DARK_OPAQUE;
            continue;
        }
        alphas.push(alpha);
        judged.push(i);
    }
    if judged.len() == 2 {
        if correlation(&alphas[0], &alphas[1]) < min_correlation {
            checks[judged[0]] = SAMPLE_DISAGREES;
            checks[judged[1]] = SAMPLE_DISAGREES;
        }
    } else if judged.len() >= 3 {
        let len = (aw * ah) as usize;
        let mut median = vec![f64::NAN; len];
        for (k, m) in median.iter_mut().enumerate() {
            let mut values: Vec<f64> = alphas.iter().map(|a| a[k]).filter(|v| !v.is_nan()).collect();
            if values.is_empty() {
                continue;
            }
            values.sort_by(|a, b| a.total_cmp(b));
            let h = values.len() / 2;
            *m = if values.len() % 2 == 1 { values[h] } else { (values[h - 1] + values[h]) / 2.0 };
        }
        for (alpha, &i) in alphas.iter().zip(&judged) {
            if correlation(alpha, &median) < min_correlation {
                checks[i] = SAMPLE_DISAGREES;
            }
        }
    }
    checks
}

// ---- 書き出す .lgd ---------------------------------------------------------------
// エンコードソフト (ソースは MIT ライセンスで公開) と同じ形: 標準の ver0.1 (見出し 32 バイト、ロゴの
// 見出し 48 バイト、画素ごとに short 6 個) の後に独自の拡張 (見出し 540 バイトと、画素ごとの係数 A・B の float)。
// エンコードソフトは拡張だけを読み、拡張の無い .lgd は読めない。標準の部分は AviUtl のロゴ消し・logoframe が読む。
//
// 係数 A・B: 8 ビットの値を 255 で割った値で「背景 = A × 見える値 + B」。見える値 = (1 − α) × 背景 + α × 色 なので
// A = 1 / (1 − α)、B = −α × 色 / (1 − α)。ロゴの無い画素は A = 1・B = 0。標準の部分の値は係数からエンコードソフトと
// 同じ計算 (ToOutLGP) で求める。

const LGD_HEADER_TEXT: &[u8] = b"<logo data file ver0.1>";
const LGD_EXTENDED_MAGIC: i32 = 0x12345;
const LGD_EXTENDED_VERSION: i32 = 1;
const LGD_MAX_DP: f32 = 1000.0;
const LGD_NAME: usize = 32;
const LGD_EXTENDED_NAME: usize = 255;
// 色差の間引き (log2。YUV 4:2:0)
const LGD_LOG_UV: i32 = 1;
// 係数を求める不透明度の上限 (1 では A が無限になる)
const LGD_MAX_FIT_ALPHA: f64 = 0.99;

fn yc48_y(y: f32) -> f32 {
    ((((y * 255.0) as i32 * 1197) >> 6) - 299) as f32
}

fn yc48_c(u: f32) -> f32 {
    ((((u * 255.0) as i32 - 128) * 4681 + 164) >> 8) as f32
}

fn yv12_y(y: f32) -> f32 {
    (((((y as i32) * 219 + 383) >> 12) + 16) as f32) / 255.0
}

fn yv12_c(u: f32) -> f32 {
    (((((u as i32) + 2048) * 7 + 66) >> 7) + 16) as f32 / 255.0
}

/// 8 ビット / 255 の係数を YC48 の直線にする (ToYC48ABY・ToYC48ABC)
fn yc48_ab(a: f32, b: f32, luma: bool) -> (f32, f32) {
    type Convert = fn(f32) -> f32;
    let (to_yv12, to_yc48): (Convert, Convert) = if luma { (yv12_y, yc48_y) } else { (yv12_c, yc48_c) };
    let (x0, x1) = (to_yv12(0.0), to_yv12(2048.0));
    let (y0, y1) = (to_yc48((x0 - b) / a), to_yc48((x1 - b) / a));
    ((y1 - y0) / 2048.0, y0)
}

/// 係数から標準の部分の (不透明度, 値) (ToOutLGP の 1 面分)。ロゴの無い画素 (A = 1・B = 0) は 0
/// (エンコードソフトの計算では輝度の丸めで不透明度 5 (0.5%)・値 0 になり、AviUtl のロゴ消しで黒に寄る)
fn out_pixel(a: f32, b: f32, luma: bool) -> (i16, i16) {
    if a == 1.0 && b == 0.0 {
        return (0, 0);
    }
    let (a, b) = yc48_ab(a, b, luma);
    if a == 1.0 {
        return (0, 0);
    }
    let value = b / (1.0 - a) + 0.5;
    if value.abs() >= 0x7FFF as f32 {
        return (0, 0);
    }
    let dp = (1.0 - a) * LGD_MAX_DP + 0.5;
    if dp.abs() > 0x3FFF as f32 || dp as i32 as i16 == 0 {
        return (0, 0);
    }
    (dp as i32 as i16, value as i32 as i16)
}

/// 書き出す .lgd 1 ファイル (ロゴ 1 つ)。name は標準の部分の名前 (cp932、31 バイトまで)、extended_name は拡張の名前
/// (cp932、254 バイトまで)。範囲はフレームの座標 (x, y, 幅 w, 高さ h。偶数)、image_w × image_h は映像の大きさ。
/// alpha・color・frames は logo_solve の面ごとの値 (float64 のバイト列。Y (h, w)、Cb・Cr (h/2, w/2))。
/// 集まったフレームが min_frames より少ない・不透明度が min_alpha 以下の画素はロゴの無い画素にする
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn lgd_file<'py>(py: Python<'py>, name: &[u8], extended_name: &[u8], service_id: i32, image_w: i32, image_h: i32,
                     x: i32, y: i32, w: usize, h: usize, alpha: Vec<Vec<u8>>, color: Vec<Vec<u8>>,
                     frames: Vec<Vec<u8>>, min_frames: f64, min_alpha: f64) -> PyResult<Bound<'py, PyBytes>> {
    let (wuv, huv) = (w >> LGD_LOG_UV, h >> LGD_LOG_UV);
    let sizes = [w * h, wuv * huv, wuv * huv];
    if alpha.len() != 3 || color.len() != 3 || frames.len() != 3 {
        return Err(pyo3::exceptions::PyValueError::new_err("three planes are needed"));
    }
    // 面ごとの係数 A・B
    let mut coef: Vec<(Vec<f32>, Vec<f32>)> = Vec::new();
    for p in 0..3 {
        let (al, co, fr) = (bytes_f64(&alpha[p]), bytes_f64(&color[p]), bytes_f64(&frames[p]));
        if al.len() != sizes[p] || co.len() != sizes[p] || fr.len() != sizes[p] {
            return Err(pyo3::exceptions::PyValueError::new_err("plane sizes do not match the logo size"));
        }
        let (mut a, mut b) = (vec![1f32; sizes[p]], vec![0f32; sizes[p]]);
        for k in 0..sizes[p] {
            if fr[k] >= min_frames && al[k] > min_alpha {
                let t = al[k].min(LGD_MAX_FIT_ALPHA);
                a[k] = (1.0 / (1.0 - t)) as f32;
                b[k] = (-t * (co[k] / 255.0) / (1.0 - t)) as f32;
            }
        }
        coef.push((a, b));
    }
    let short = |v: i32| (v as i16).to_le_bytes();
    let mut out: Vec<u8> = Vec::new();
    // 標準の部分: ファイルの見出し (ロゴの数は 4 バイトの大きい桁から)、ロゴの見出し、画素
    let mut head = LGD_HEADER_TEXT.to_vec();
    head.resize(28, 0);
    out.extend(head);
    out.extend([0, 0, 0, 1]);
    let mut base_name = name[..name.len().min(LGD_NAME - 1)].to_vec();
    base_name.resize(LGD_NAME, 0);
    out.extend(base_name);
    for v in [x, y, h as i32, w as i32, 0, 0, 0, 0] {
        out.extend(short(v));
    }
    for yy in 0..h {
        for xx in 0..w {
            let (k, kuv) = (yy * w + xx, (yy >> LGD_LOG_UV) * wuv + (xx >> LGD_LOG_UV));
            let (dy, vy) = out_pixel(coef[0].0[k], coef[0].1[k], true);
            let (du, vu) = out_pixel(coef[1].0[kuv], coef[1].1[kuv], false);
            let (dv, vv) = out_pixel(coef[2].0[kuv], coef[2].1[kuv], false);
            for v in [dy, vy, du, vu, dv, vv] {
                out.extend(v.to_le_bytes());
            }
        }
    }
    // 拡張: 見出し (識別値・版・大きさ・色差の間引き・映像の大きさ・位置・名前 255 バイト・詰め物 1 バイト・サービス ID・予備)
    for v in [LGD_EXTENDED_MAGIC, LGD_EXTENDED_VERSION, w as i32, h as i32, LGD_LOG_UV, LGD_LOG_UV, image_w, image_h, x, y] {
        out.extend(v.to_le_bytes());
    }
    let mut ext_name = extended_name[..extended_name.len().min(LGD_EXTENDED_NAME - 1)].to_vec();
    ext_name.resize(LGD_EXTENDED_NAME + 1, 0);
    out.extend(ext_name);
    out.extend(service_id.to_le_bytes());
    out.extend([0u8; 240]);
    // 係数: aY, bY, aU, bU, aV, bV
    for (a, b) in &coef {
        for v in a.iter().chain(b.iter()) {
            out.extend(v.to_le_bytes());
        }
    }
    Ok(PyBytes::new(py, &out))
}
