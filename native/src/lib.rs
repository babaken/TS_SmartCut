//! 特徴の計算 (ts_smartcut/autocut/features.py) の、録画を読んでいる間の処理。
//! (2026-10-28 から必須。それまで同じ処理を Python でも持っていた)
//!
//! - 録画を読み、TS を解析しながら ffmpeg (と、ロゴ画像の材料の ffmpeg) の標準入力へ渡す
//! - ffmpeg の灰色のフレームごとに: 場面の切り替わり、四隅の集計、1/8 の四隅を一時ファイルへ、ロゴの型の縁の向き
//! - ロゴ画像の材料: 残した区間のキーフレームを一時ファイルへ (どのフレームを残すかは Python の乱数で決める)
//!
//! ffmpeg の起動・止める・終わりを待つのは Python のまま。パイプは Python が複製したハンドル
//! (Linux はファイル記述子) を受け取り、このモジュールが閉じる。
//!
//! 読み終わった後の特徴の計算 (音量・コマとフレーム・四隅・ロゴの型と一致度・ロゴ画像の材料) は post.rs。
//! ルールによる案づくり (境目の候補・区間の判定・案・局の値・正解との比較) は rules.rs、しきい値の自動補正
//! (tuning.py) の評価 1 回分は tuning.rs、番組情報 (broadcast/program_info.py) の読み込みは
//! program_info.rs (ARIB の文字は arib.rs)、TS のパケットとセクションの読み方は ts.rs (特徴の計算と共通)、
//! 部分ごとに切り出した H.264 をつなぐ時の POC の書き換え (ffmpeg/smart_cut.py) は h264.rs。

mod arib;
mod arib_tables;
mod gbdt;
mod h264;
mod logo;
mod post;
mod program_info;
mod rules;
mod scanner;
mod ts;
mod tuning;

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use scanner::Scanner;
use ts::Stream;
use std::fs::File;
use std::io::{BufWriter, Read, Seek, SeekFrom, Write};
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{SyncSender, sync_channel};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

// 録画を読んで ffmpeg に渡す 1 回の量 (features._READ_SIZE)
const READ_SIZE: usize = 4 * 1024 * 1024;
// ロゴ画像の材料の ffmpeg へ渡す前に溜めておける数 (features._EXTRA_QUEUE)
const EXTRA_QUEUE: usize = 8;
// 進み具合を知らせ、止めるかを尋ねる間隔 (features._CHUNK_FRAMES)
const TICK_FRAMES: usize = 128;
// 縁とみなす明るさの差 (logo.EDGE)
const EDGE: i16 = 2;

/// Python から受け取ったパイプのハンドルを File にする (以後このモジュールが閉じる)
fn pipe(handle: i64) -> File {
    #[cfg(windows)]
    {
        use std::os::windows::io::{FromRawHandle, RawHandle};
        unsafe { File::from_raw_handle(handle as isize as RawHandle) }
    }
    #[cfg(unix)]
    {
        use std::os::unix::io::FromRawFd;
        unsafe { File::from_raw_fd(handle as i32) }
    }
}

/// 残した区間 [start, end)。excluded は基準外
#[derive(Clone, Copy)]
struct Keep {
    start: i64,
    end: i64,
    excluded: bool,
}

/// フレームの区別: 1 = 残した区間、0 = それ以外、-1 = 基準外。後の区間が優先 (features._labels_for)
fn label(frame_no: i64, keep: &[Keep]) -> i8 {
    let mut l = 0;
    for r in keep {
        if frame_no >= r.start && frame_no < r.end {
            l = if r.excluded { -1 } else { 1 };
        }
    }
    l
}

fn frame_number(time: f64, video_start: f64, fps: f64) -> i64 {
    ((time - video_start) * fps + 0.5).floor() as i64
}

/// ffmpeg が -stats_mux_pre で 1 行ずつ書く「時刻 時間の単位」を読む
struct Stats {
    path: PathBuf,
    file: Option<File>,
    partial: String,
    values: Vec<f64>,
}

impl Stats {
    fn new(path: PathBuf) -> Self {
        Stats { path, file: None, partial: String::new(), values: Vec::new() }
    }

    fn add(&mut self, line: &str) {
        let mut parts = line.split_whitespace();
        let (Some(value), Some(tb), None) = (parts.next(), parts.next(), parts.next()) else { return };
        let Ok(value) = value.parse::<i64>() else { return };
        let (num, den) = tb.split_once('/').unwrap_or((tb, "1"));
        let (Ok(num), Ok(den)) = (num.parse::<i64>(), if den.is_empty() { Ok(1) } else { den.parse::<i64>() }) else {
            return;
        };
        self.values.push((value * num) as f64 / den as f64);
    }

    /// 増えた分を読む。読めたら true
    fn poll(&mut self) -> bool {
        if self.file.is_none() {
            self.file = File::open(&self.path).ok();
        }
        let Some(f) = self.file.as_mut() else { return false };
        let mut chunk = Vec::new();
        if f.read_to_end(&mut chunk).unwrap_or(0) == 0 {
            return false;
        }
        let text = std::mem::take(&mut self.partial) + &String::from_utf8_lossy(&chunk);
        let mut lines: Vec<&str> = text.split('\n').collect();
        self.partial = lines.pop().unwrap_or("").to_string();
        for line in lines {
            self.add(line);
        }
        true
    }

    /// count 個の時刻が読めるまで待つ。alive が false (ffmpeg が終わった・止める) なら、そこまでで返す
    fn read_until(&mut self, count: usize, alive: &mut dyn FnMut() -> bool) {
        while self.values.len() < count {
            if self.poll() {
                continue;
            }
            if !alive() {
                if !self.partial.trim().is_empty() {
                    let line = std::mem::take(&mut self.partial);
                    self.add(&line);
                }
                return;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
    }
}

/// alive() を尋ねる (GIL を取って)。例外なら動いていないとみなす
fn ask_alive(alive: &Py<PyAny>) -> bool {
    Python::attach(|py| alive.call0(py).and_then(|v| v.extract::<bool>(py)).unwrap_or(false))
}

/// tick(フレーム数) を呼ぶ (GIL を取って)。True なら止める
fn ask_tick(tick: &Py<PyAny>, pictures: usize) -> PyResult<bool> {
    Python::attach(|py| tick.call1(py, (pictures,)).and_then(|v| v.extract::<bool>(py)))
}

#[derive(Clone, Copy)]
struct Geometry {
    w: usize,
    h: usize,
    cw: usize,
    ch: usize,
    lw: usize,
    lh: usize,
}

impl Geometry {
    fn corner_origin(&self, c: usize) -> (usize, usize) {
        (if c.is_multiple_of(2) { 0 } else { self.w - self.cw }, if c < 2 { 0 } else { self.h - self.ch })
    }
}

/// 読み込みの時間の内訳 (秒。finish() の "timing"。速さの測定用で、計算には使わない)
#[derive(Default, Clone)]
struct Timing {
    values: Vec<(&'static str, f64)>,
}

impl Timing {
    fn add(&mut self, name: &'static str, since: Instant) {
        let seconds = since.elapsed().as_secs_f64();
        match self.values.iter_mut().find(|(n, _)| *n == name) {
            Some((_, v)) => *v += seconds,
            None => self.values.push((name, seconds)),
        }
    }
}

/// 灰色のフレームの計算 (features._compute_features の読み込みのループ)
struct Frames {
    video_out: File,
    stats: Stats,
    g: Geometry,
    keep: Vec<Keep>,
    video_start: f64,
    fps: f64,
    logo_file: BufWriter<File>,
    diffs: Vec<f32>,
    pts: Vec<f64>,
    kept_sum: Vec<u64>,
    kept_sq: Vec<u64>,
    other_sum: Vec<u64>,
    other_sq: Vec<u64>,
    kept_n: u64,
    other_n: u64,
    sum_x: Vec<i64>,
    sum_y: Vec<i64>,
    template_count: u64,
    stopped: bool,
    error: Option<String>,
    timing: Timing,
}

impl Frames {
    /// tick が例外を出したら止めて、その例外を返す
    fn run(&mut self, tick: &Py<PyAny>, alive: &Py<PyAny>, stop: &AtomicBool) -> Option<PyErr> {
        let g = self.g;
        let (w, h, cw, ch, lw, lh) = (g.w, g.h, g.cw, g.ch, g.lw, g.lh);
        let (sw, sh) = (w.div_ceil(2), h.div_ceil(2));
        let mut buf = vec![0u8; w * h];
        let mut small = vec![0u8; sw * sh];
        let mut prev = vec![0u8; sw * sh];
        let mut logo = vec![0u8; 4 * lh * lw];
        let mut alive_fn = || ask_alive(alive);
        let mut failure = None;
        let tick_fn = |pictures: usize, failure: &mut Option<PyErr>| match ask_tick(tick, pictures) {
            Ok(stop) => stop,
            Err(e) => {
                *failure = Some(e);
                true
            }
        };
        self.stopped = tick_fn(0, &mut failure);
        let started = Instant::now();
        while !self.stopped {
            let waiting = Instant::now();
            let read = self.video_out.read_exact(&mut buf);
            self.timing.add("frames_wait_ffmpeg", waiting);
            match read {
                Ok(()) => {}
                Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => break,
                Err(e) => {
                    // 読めなくなったら止めた時と同じに扱う (Python が ffmpeg をすぐ終わらせる。
                    // 止めないと ffmpeg は誰も読まない出力に書こうとして止まり、60 秒待ってから終わった)
                    self.error = Some(e.to_string());
                    self.stopped = true;
                    break;
                }
            }
            let pictures = self.diffs.len();
            let waiting = Instant::now();
            self.stats.read_until(pictures + 1, &mut alive_fn);
            self.timing.add("frames_wait_stats", waiting);
            if self.stats.values.len() <= pictures {
                break;
            }
            let time = self.stats.values[pictures];
            let computing = Instant::now();

            // 場面の切り替わり: 1 画素おきに間引いた画像の差の平均
            for (sy, y) in (0..h).step_by(2).enumerate() {
                let row = &buf[y * w..y * w + w];
                for (o, v) in small[sy * sw..sy * sw + sw].iter_mut().zip(row.iter().step_by(2)) {
                    *o = *v;
                }
            }
            let d = if pictures == 0 {
                0.0
            } else {
                let sum: u64 = prev.iter().zip(&small).map(|(a, b)| a.abs_diff(*b) as u64).sum();
                sum as f64 / small.len() as f64
            };
            self.diffs.push(d as f32);
            std::mem::swap(&mut prev, &mut small);

            // 四隅: 残した区間とそれ以外に分けて集計
            let l = label(frame_number(time, self.video_start, self.fps), &self.keep);
            if l >= 0 {
                let (sum, sq) = if l == 1 {
                    self.kept_n += 1;
                    (&mut self.kept_sum, &mut self.kept_sq)
                } else {
                    self.other_n += 1;
                    (&mut self.other_sum, &mut self.other_sq)
                };
                for c in 0..4 {
                    let (x0, y0) = g.corner_origin(c);
                    for y in 0..ch {
                        let row = &buf[(y0 + y) * w + x0..(y0 + y) * w + x0 + cw];
                        let base = (c * ch + y) * cw;
                        for (x, &v) in row.iter().enumerate() {
                            sum[base + x] += v as u64;
                            sq[base + x] += (v as u64) * (v as u64);
                        }
                    }
                }
            }

            // ロゴ: 四隅を 1/2 (2×2 の平均) にして一時ファイルへ (logo.downscale_corners)。
            // 残した区間なら縁の向きを数える (logo.TemplateAccumulator)
            for c in 0..4 {
                let (x0, y0) = g.corner_origin(c);
                for y in 0..lh {
                    let r0 = (y0 + 2 * y) * w + x0;
                    let r1 = r0 + w;
                    for x in 0..lw {
                        let s = buf[r0 + 2 * x] as u16 + buf[r1 + 2 * x] as u16 + buf[r0 + 2 * x + 1] as u16
                            + buf[r1 + 2 * x + 1] as u16;
                        logo[(c * lh + y) * lw + x] = ((s + 2) / 4) as u8;
                    }
                }
            }
            let writing = Instant::now();
            if let Err(e) = self.logo_file.write_all(&logo) {
                // 一時ファイルが書けない (ディスクが一杯など) 時も、止めた時と同じに扱う
                self.error = Some(e.to_string());
                self.stopped = true;
                break;
            }
            self.timing.add("frames_write_corners", writing);
            if l == 1 {
                let sign = |a: u8, b: u8| -> i64 {
                    let d = b as i16 - a as i16;
                    (d >= EDGE) as i64 - (d <= -EDGE) as i64
                };
                let lw1 = lw.saturating_sub(1);
                let lh1 = lh.saturating_sub(1);
                for c in 0..4 {
                    let base = c * lh * lw;
                    for y in 0..lh {
                        for x in 0..lw1 {
                            let i = base + y * lw + x;
                            self.sum_x[(c * lh + y) * lw1 + x] += sign(logo[i], logo[i + 1]);
                        }
                    }
                    for y in 0..lh1 {
                        for x in 0..lw {
                            let i = base + y * lw + x;
                            self.sum_y[(c * lh1 + y) * lw + x] += sign(logo[i], logo[i + lw]);
                        }
                    }
                }
                self.template_count += 1;
            }
            self.pts.push(time);
            self.timing.add("frames_compute", computing);

            let done = self.diffs.len();
            let asking = Instant::now();
            if done.is_multiple_of(TICK_FRAMES) && tick_fn(done, &mut failure) {
                self.stopped = true;
            }
            self.timing.add("frames_tick", asking);
        }
        self.timing.add("frames_total", started);
        if self.stopped {
            stop.store(true, Ordering::Release);
        } else if !self.diffs.len().is_multiple_of(TICK_FRAMES) {
            self.stopped = tick_fn(self.diffs.len(), &mut failure);
        }
        if let Err(e) = self.logo_file.flush() {
            // 終わりにまとめて書く分が書けない時も、途中まで (ロゴの値が欠ける) として止めた扱いにする
            self.error.get_or_insert(e.to_string());
            if !self.stopped {
                self.stopped = true;
                stop.store(true, Ordering::Release);
            }
        }
        failure
    }
}

/// 録画を読んで渡すスレッドの結果
#[derive(Default)]
struct Feed {
    complete: bool,
    error: Option<String>,
    scan_error: Option<String>,
    scanner: Option<Scanner>,
    timing: Timing,
}

/// 渡す口と、終わりを待つ口 (渡せたかと時間の内訳)
type ImageWriter = (SyncSender<Arc<Vec<u8>>>, JoinHandle<(bool, Timing)>);

/// ロゴ画像の材料の ffmpeg へ渡すスレッド。受け取らなくなったら閉じて、以後は渡さない
fn image_writer(mut image_in: File) -> ImageWriter {
    let (tx, rx) = sync_channel::<Arc<Vec<u8>>>(EXTRA_QUEUE);
    let thread = std::thread::Builder::new()
        .name("autocut-feeder-extra".into())
        .spawn(move || {
            let mut timing = Timing::default();
            for chunk in rx {
                let writing = Instant::now();
                let ok = image_in.write_all(&chunk).is_ok();
                timing.add("image_feed_blocked", writing);
                if !ok {
                    return (false, timing);
                }
            }
            (true, timing)
        })
        .expect("thread");
    (tx, thread)
}

/// 録画を読み、TS を解析しながら ffmpeg へ渡す
fn feed(file_path: PathBuf, mut video_in: File, image_in: Option<File>, mut scanner: Scanner, stop: Arc<AtomicBool>)
    -> (Feed, bool) {
    let mut result = Feed::default();
    let mut image = image_in.map(image_writer);
    let mut image_ok = true;
    let mut image_timing = Timing::default();
    let started = Instant::now();
    match File::open(&file_path) {
        Err(e) => result.error = Some(e.to_string()),
        Ok(mut f) => {
            while !stop.load(Ordering::Acquire) {
                let mut data = vec![0u8; READ_SIZE];
                let reading = Instant::now();
                let read = f.read(&mut data);
                result.timing.add("feed_read_file", reading);
                let n = match read {
                    Ok(n) => n,
                    Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                    Err(e) => {
                        result.error = Some(e.to_string());
                        break;
                    }
                };
                if n == 0 {
                    result.complete = true;
                    break;
                }
                data.truncate(n);
                let scanning = Instant::now();
                if let Err(e) = scanner.feed_bytes(&data) {
                    // 解析の失敗で映像・音声の計算は止めない
                    result.scan_error.get_or_insert(format!("{e:?}"));
                }
                result.timing.add("feed_scan", scanning);
                let data = Arc::new(data);
                let sending = Instant::now();
                if let Some((tx, _)) = image.as_ref()
                    && tx.send(data.clone()).is_err() {
                        // 受け取るスレッドが終わった (ffmpeg が受け取らなくなった)。以後は渡さない
                        if let Some((tx, thread)) = image.take() {
                            drop(tx);
                            (image_ok, image_timing) = thread.join().unwrap_or_default();
                        }
                    }
                result.timing.add("feed_image_queue_blocked", sending);
                let writing = Instant::now();
                let written = video_in.write_all(&data).is_ok();
                result.timing.add("feed_video_blocked", writing);
                if !written {
                    break; // ffmpeg が先に終わった (止めた・エラー)
                }
            }
        }
    }
    drop(video_in);
    if let Some((tx, thread)) = image.take() {
        drop(tx);
        (image_ok, image_timing) = thread.join().unwrap_or_default();
    }
    result.timing.add("feed_total", started);
    result.timing.values.extend(image_timing.values);
    result.scanner = Some(scanner);
    (result, image_ok)
}

/// ロゴ画像の材料の結果
#[derive(Default)]
struct Images {
    count: u64,
    pictures: u64,
    error: Option<String>,
    timing: Timing,
}

/// 残した区間のキーフレームを最大 max_pictures 枚、一時ファイルに残す (ffmpeg は features._LogoImageReader)。
/// max_pictures 枚より後は、choose_slot (Python の乱数) で置き換える位置を決める
#[allow(clippy::too_many_arguments)]
fn collect_images(mut image_out: File, mut stats: Stats, mosaic_bytes: usize, keep: Vec<Keep>, video_start: f64,
                  fps: f64, spool_path: PathBuf, max_pictures: u64, alive: Py<PyAny>, choose_slot: Py<PyAny>)
    -> Images {
    let mut result = Images::default();
    let mut alive_fn = || ask_alive(&alive);
    let mut run = |result: &mut Images, image_out: &mut File, alive_fn: &mut dyn FnMut() -> bool|
        -> Result<(), String> {
        let mut spool = File::create(&spool_path).map_err(|e| e.to_string())?;
        let mut buf = vec![0u8; mosaic_bytes];
        let mut seen = 0usize;
        loop {
            let waiting = Instant::now();
            let read = image_out.read_exact(&mut buf);
            result.timing.add("image_wait_ffmpeg", waiting);
            if read.is_err() {
                break;
            }
            seen += 1;
            stats.read_until(seen, alive_fn);
            if stats.values.len() < seen {
                break;
            }
            if label(frame_number(stats.values[seen - 1], video_start, fps), &keep) != 1 {
                continue;
            }
            let i = result.pictures;
            result.pictures += 1;
            let slot = if i < max_pictures {
                i
            } else {
                Python::attach(|py| choose_slot.call1(py, (i + 1,)).and_then(|v| v.extract::<u64>(py)))
                    .map_err(|e| e.to_string())?
            };
            if slot < max_pictures {
                spool.seek(SeekFrom::Start(slot * mosaic_bytes as u64)).map_err(|e| e.to_string())?;
                spool.write_all(&buf).map_err(|e| e.to_string())?;
                result.count = result.count.max(slot + 1);
            }
        }
        Ok(())
    };
    if let Err(e) = run(&mut result, &mut image_out, &mut alive_fn) {
        result.error = Some(e);
    }
    // 読み込みが止まると録画を渡しているスレッドも止まるので、ffmpeg の出力は最後まで読み捨てる
    let mut sink = vec![0u8; 1 << 16];
    while image_out.read(&mut sink).map(|n| n > 0).unwrap_or(false) {}
    result
}

/// 特徴の計算の読み込み (features._compute_features から使う)
#[pyclass(module = "ts_smartcut_native")]
struct FeatureReader {
    frames: Option<Frames>,
    stop: Arc<AtomicBool>,
    feeder: Option<JoinHandle<(Feed, bool)>>,
    images: Option<JoinHandle<Images>>,
    read: bool,
}

fn bytes_of<T: Copy>(py: Python<'_>, values: &[T], to_le: impl Fn(T) -> Vec<u8>) -> Py<PyBytes> {
    let mut out = Vec::with_capacity(std::mem::size_of_val(values));
    for &v in values {
        out.extend(to_le(v));
    }
    PyBytes::new(py, &out).unbind()
}

#[pymethods]
impl FeatureReader {
    /// 録画を読んで渡すスレッドと、ロゴ画像の材料を読むスレッドを始める。
    /// video_in・video_out・image_in・image_out は複製したパイプのハンドル (このモジュールが閉じる)。
    /// keep は (start, end, excluded) の並び。image_* は、ロゴ画像の材料を読まない時は None
    #[new]
    #[pyo3(signature = (file_path, video_in, video_out, stats_path, width, height, corner_width, corner_height,
                        video_start, fps, keep, service_id, logo_path, image_in=None, image_out=None,
                        image_stats_path=None, mosaic_bytes=0, spool_path=None, max_pictures=0,
                        image_alive=None, choose_slot=None))]
    #[allow(clippy::too_many_arguments)]
    fn new(file_path: PathBuf, video_in: i64, video_out: i64, stats_path: PathBuf, width: usize, height: usize,
           corner_width: usize, corner_height: usize, video_start: f64, fps: f64, keep: Vec<(i64, i64, bool)>,
           service_id: Option<u16>, logo_path: PathBuf, image_in: Option<i64>, image_out: Option<i64>,
           image_stats_path: Option<PathBuf>, mosaic_bytes: usize, spool_path: Option<PathBuf>, max_pictures: u64,
           image_alive: Option<Py<PyAny>>, choose_slot: Option<Py<PyAny>>) -> PyResult<Self> {
        // ハンドルは最初に受け取る (以後、失敗してもここで閉じる)
        let video_in = pipe(video_in);
        let video_out = pipe(video_out);
        let image_in = image_in.map(pipe);
        let image_out = image_out.map(pipe);
        let keep: Vec<Keep> = keep.into_iter().map(|(start, end, excluded)| Keep { start, end, excluded }).collect();
        let (cw, ch) = (corner_width, corner_height);
        if cw > width || ch > height || width == 0 || height == 0 {
            return Err(PyRuntimeError::new_err("corner size larger than the frame"));
        }
        let g = Geometry { w: width, h: height, cw, ch, lw: cw / 2, lh: ch / 2 };
        let logo_file = BufWriter::with_capacity(8 << 20, File::create(&logo_path)?);
        let corner_px = 4 * ch * cw;
        let frames = Frames {
            video_out,
            stats: Stats::new(stats_path),
            g,
            keep: keep.clone(),
            video_start,
            fps,
            logo_file,
            diffs: Vec::new(),
            pts: Vec::new(),
            kept_sum: vec![0; corner_px],
            kept_sq: vec![0; corner_px],
            other_sum: vec![0; corner_px],
            other_sq: vec![0; corner_px],
            kept_n: 0,
            other_n: 0,
            sum_x: vec![0; 4 * g.lh * g.lw.saturating_sub(1)],
            sum_y: vec![0; 4 * g.lh.saturating_sub(1) * g.lw],
            template_count: 0,
            stopped: false,
            error: None,
            timing: Timing::default(),
        };
        let stop = Arc::new(AtomicBool::new(false));
        let images = match (image_out, image_stats_path, spool_path, image_alive, choose_slot) {
            (Some(out), Some(stats), Some(spool), Some(alive), Some(choose)) => {
                let keep = keep.clone();
                Some(
                    std::thread::Builder::new()
                        .name("autocut-logo-image".into())
                        .spawn(move || {
                            collect_images(out, Stats::new(stats), mosaic_bytes, keep, video_start, fps, spool,
                                           max_pictures, alive, choose)
                        })?,
                )
            }
            _ => None,
        };
        let scanner = Scanner::new(service_id, video_start);
        let feeder = {
            let stop = stop.clone();
            std::thread::Builder::new()
                .name("autocut-feeder".into())
                .spawn(move || feed(file_path, video_in, image_in, scanner, stop))?
        };
        Ok(FeatureReader { frames: Some(frames), stop, feeder: Some(feeder), images, read: false })
    }

    /// ffmpeg のフレームを最後まで (止めるまで) 読んで計算する。tick(フレーム数) は 128 フレームごとに
    /// 呼び、True を返したら止める。alive() は時刻の行を待つ時に、ffmpeg が動いているかを尋ねる。
    /// 止めたら True を返す (読み込み・一時ファイルの書き込みに失敗した時も。この後 ffmpeg を
    /// 終わらせてから finish を呼ぶ)
    fn read_frames(&mut self, py: Python<'_>, tick: Py<PyAny>, alive: Py<PyAny>) -> PyResult<bool> {
        if self.read {
            return Err(PyRuntimeError::new_err("read_frames was already called"));
        }
        self.read = true;
        let mut frames = self.frames.take().expect("frames");
        let stop = self.stop.clone();
        let (frames, failure) = py.detach(move || {
            let failure = frames.run(&tick, &alive, &stop);
            (frames, failure)
        });
        let stopped = frames.stopped;
        self.frames = Some(frames);
        match failure {
            Some(e) => Err(e),
            None => Ok(stopped),
        }
    }

    /// スレッドの終わりを待ち、結果を dict で返す
    fn finish<'py>(&mut self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let frames = self.frames.take().ok_or_else(|| PyRuntimeError::new_err("finish was already called"))?;
        self.stop.store(true, Ordering::Release);
        let feeder = self.feeder.take();
        let images = self.images.take();
        let ((feed, image_ok), images) = py.detach(move || {
            let feed = feeder.map(|t| t.join().unwrap_or_default()).unwrap_or_default();
            let images = images.map(|t| t.join().unwrap_or_default());
            (feed, images)
        });
        let Frames { diffs, pts, kept_sum, kept_sq, other_sum, other_sq, kept_n, other_n, sum_x, sum_y,
                     template_count, stopped, error, timing, .. } = frames;
        let d = PyDict::new(py);
        // 時間の内訳 (速さの測定用。features.py は使わない)
        let timing_dict = PyDict::new(py);
        let image_timing = images.as_ref().map(|i| i.timing.values.clone()).unwrap_or_default();
        for (name, seconds) in timing.values.iter().chain(&feed.timing.values).chain(&image_timing) {
            timing_dict.set_item(*name, *seconds)?;
        }
        d.set_item("timing", timing_dict)?;
        let f64s = |v: &[u64]| -> Vec<f64> { v.iter().map(|&x| x as f64).collect() };
        d.set_item("diffs", bytes_of(py, &diffs, |v: f32| v.to_le_bytes().to_vec()))?;
        d.set_item("pts", bytes_of(py, &pts, |v: f64| v.to_le_bytes().to_vec()))?;
        for (name, values) in
            [("kept_sum", &kept_sum), ("kept_sq", &kept_sq), ("other_sum", &other_sum), ("other_sq", &other_sq)]
        {
            d.set_item(name, bytes_of(py, &f64s(values), |v: f64| v.to_le_bytes().to_vec()))?;
        }
        d.set_item("kept_n", kept_n)?;
        d.set_item("other_n", other_n)?;
        d.set_item("template_sum_x", bytes_of(py, &sum_x, |v: i64| v.to_le_bytes().to_vec()))?;
        d.set_item("template_sum_y", bytes_of(py, &sum_y, |v: i64| v.to_le_bytes().to_vec()))?;
        d.set_item("template_count", template_count)?;
        d.set_item("stopped", stopped)?;
        d.set_item("read_error", error)?;
        d.set_item("feed_complete", feed.complete)?;
        d.set_item("feed_error", feed.error)?;
        d.set_item("scan_error", feed.scan_error)?;
        d.set_item("image_input_ok", image_ok)?;
        let scanner = feed.scanner;
        let pmt: Vec<(f64, Vec<Stream>)> = scanner.as_ref().map(|s| s.pmt_changes.clone()).unwrap_or_default();
        d.set_item("pmt_changes", pmt)?;
        let channels: Vec<(f64, u8)> = scanner.as_ref().map(|s| s.audio_channels.clone()).unwrap_or_default();
        d.set_item("audio_channels", channels)?;
        let captions: Vec<f64> = scanner.as_ref().map(|s| s.captions.clone()).unwrap_or_default();
        d.set_item("captions", bytes_of(py, &captions, |v: f64| v.to_le_bytes().to_vec()))?;
        if let Some(images) = images {
            d.set_item("image_count", images.count)?;
            d.set_item("image_pictures", images.pictures)?;
            d.set_item("image_error", images.error)?;
        }
        Ok(d)
    }
}

type ScanResult = (Vec<(f64, Vec<Stream>)>, Vec<(f64, u8)>, Vec<f64>);

/// TS の解析だけ (stream_events.scan_stream_events と、解析のテスト用)
#[pyclass(module = "ts_smartcut_native", name = "StreamScanner")]
struct PyStreamScanner {
    scanner: Scanner,
}

#[pymethods]
impl PyStreamScanner {
    #[new]
    #[pyo3(signature = (service_id, video_start))]
    fn new(service_id: Option<u16>, video_start: f64) -> Self {
        PyStreamScanner { scanner: Scanner::new(service_id, video_start) }
    }

    /// 続きのデータを解析する。解析に失敗したら、その理由 (このデータの残りは見ない)
    fn feed_bytes(&mut self, data: &[u8]) -> Option<String> {
        self.scanner.feed_bytes(data).err().map(|e| format!("{e:?}"))
    }

    /// (PMT の変化, チャンネル数の変化, 字幕の時刻)
    fn result(&self) -> ScanResult {
        let s = &self.scanner;
        (s.pmt_changes.clone(), s.audio_channels.clone(), s.captions.clone())
    }
}

#[pymodule]
fn ts_smartcut_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<FeatureReader>()?;
    m.add_class::<PyStreamScanner>()?;
    m.add_class::<tuning::TuneSample>()?;
    m.add_class::<gbdt::GbdtModel>()?;
    m.add_function(wrap_pyfunction!(tuning::evaluate_samples, m)?)?;
    for f in [
        wrap_pyfunction!(post::audio_peaks, m)?,
        wrap_pyfunction!(post::peaks_to_levels, m)?,
        wrap_pyfunction!(post::audio_levels_from_file, m)?,
        wrap_pyfunction!(post::increasing_pictures, m)?,
        wrap_pyfunction!(post::map_pictures_to_frames, m)?,
        wrap_pyfunction!(post::picture_frames, m)?,
        wrap_pyfunction!(post::corner_mean_std, m)?,
        wrap_pyfunction!(post::logo_template, m)?,
        wrap_pyfunction!(post::logo_sums_from_corners, m)?,
        wrap_pyfunction!(post::logo_scores_from_file, m)?,
        wrap_pyfunction!(post::logo_scores_from_corners, m)?,
        wrap_pyfunction!(post::logo_image_samples, m)?,
        wrap_pyfunction!(post::logo_boxes, m)?,
        wrap_pyfunction!(post::frame_scores, m)?,
        wrap_pyfunction!(logo::logo_combine, m)?,
        wrap_pyfunction!(logo::logo_agreement, m)?,
        wrap_pyfunction!(logo::logo_two_way_agreement, m)?,
        wrap_pyfunction!(logo::lgd_template, m)?,
        wrap_pyfunction!(logo::logo_solve, m)?,
        wrap_pyfunction!(logo::logo_sample_checks, m)?,
        wrap_pyfunction!(logo::lgd_file, m)?,
    ] {
        m.add_function(f)?;
    }
    m.add_class::<rules::RuleRecording>()?;
    m.add_function(wrap_pyfunction!(rules::rule_boundaries, m)?)?;
    m.add_function(wrap_pyfunction!(rules::rule_segments, m)?)?;
    m.add_function(wrap_pyfunction!(rules::rule_classify, m)?)?;
    m.add_function(wrap_pyfunction!(rules::rule_segment_values, m)?)?;
    m.add_function(wrap_pyfunction!(rules::rule_assemble, m)?)?;
    m.add_function(wrap_pyfunction!(rules::rule_measures, m)?)?;
    m.add_function(wrap_pyfunction!(rules::rule_profile, m)?)?;
    m.add_function(wrap_pyfunction!(rules::rule_compare, m)?)?;
    m.add_function(wrap_pyfunction!(program_info::read_program_info_raw, m)?)?;
    m.add_function(wrap_pyfunction!(program_info::decode_arib_text, m)?)?;
    m.add_function(wrap_pyfunction!(h264::h264_tail_order, m)?)?;
    m.add_function(wrap_pyfunction!(h264::h264_continue_order, m)?)?;
    m.add("THRESHOLD_NAMES", rules::THRESHOLD_NAMES.to_vec())?;
    // 計算の方法を変えたら上げる (ts_smartcut/autocut/native.py の NATIVE_VERSION も。合わない時は E506)。
    // main に出す PR ごとに 1 回。マージ前の PR の中で関数を足した時は上げ直さない (2026-10-31)
    m.add("VERSION", 10)?;
    Ok(())
}
