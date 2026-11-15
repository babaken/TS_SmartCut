//! 学習による区間の判定のモデル: 勾配ブースティング決定木 (設計書 §31.9、段階 ④。2026-10-30 までは LightGBM)。
//!
//! LightGBM と同じ考え方の小さな実装 (model.py の _PARAMS と同じ設定で使う):
//! - 二値の分類 (対数損失)、行ごとの重み、初めの値は重み付きの正解の割合の対数オッズ
//! - 特徴の値を最大 MAX_BINS 個の区切りにまとめたヒストグラムで分け方を探す。葉ごとに伸ばす (利得が最大の葉を
//!   num_leaves になるまで分ける)。小さい方の子だけヒストグラムを作り、大きい方は親から引く
//! - 欠けた値 (NaN) は数値の特徴では左右の良い方へ (分け方ごとに覚える)、カテゴリの特徴では右へ
//! - カテゴリの特徴: 値の種類が MAX_CAT_TO_ONEHOT 以下なら 1 つ対残り、多ければ勾配 / (ヘシアン + CAT_SMOOTH) の
//!   順に並べた先頭 (両端から MAX_CAT_THRESHOLD まで) を左にする。利得には CAT_L2 を足す
//! - 毎回、行の bagging_fraction・特徴の feature_fraction を種の決まった乱数で選ぶ (同じ入力なら毎回同じモデル)
//!
//! モデルは文字列で保存する (to_text / from_text。数値は Rust の往復できる書き方)。予測は元の値で木をたどるので、
//! 区切りの表は保存しない。

use crate::post::{bytes_f64, to_bytes};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

const MAX_BINS: usize = 255;
const MIN_SUM_HESSIAN: f64 = 1e-3;
const MIN_GAIN: f64 = 1e-12;
const CAT_SMOOTH: f64 = 10.0;
const CAT_L2: f64 = 10.0;
const MAX_CAT_THRESHOLD: usize = 32;
const MAX_CAT_TO_ONEHOT: usize = 4;
const TEXT_HEADER: &str = "ts_smartcut_gbdt 1";

/// 学習の設定 (model.py の _PARAMS・_ROUNDS)
#[derive(Clone)]
struct Params {
    rounds: usize,
    learning_rate: f64,
    num_leaves: usize,
    min_data_in_leaf: usize,
    lambda_l2: f64,
    feature_fraction: f64,
    bagging_fraction: f64,
    bagging_freq: usize,
    seed: u64,
}

/// 種の決まった乱数 (SplitMix64)
struct Rng(u64);

impl Rng {
    fn next_u64(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    fn uniform(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 / (1u64 << 53) as f64
    }

    fn below(&mut self, n: usize) -> usize {
        (self.uniform() * n as f64) as usize % n.max(1)
    }
}

// ---- 木とモデル -------------------------------------------------------------------

#[derive(Clone, Debug, PartialEq)]
enum Split {
    /// 値 <= threshold なら左。欠けた値は missing_left
    Numeric { threshold: f64, missing_left: bool },
    /// 値が left の中なら左 (欠けた値・知らない値は右)
    Categorical { left: Vec<i64> },
}

impl Split {
    fn goes_left(&self, v: f64) -> bool {
        match self {
            Split::Numeric { threshold, missing_left } => {
                if v.is_nan() {
                    *missing_left
                } else {
                    v <= *threshold
                }
            }
            Split::Categorical { left } => category(v).is_some_and(|c| left.contains(&c)),
        }
    }
}

/// カテゴリの値 (0 以上の整数でなければ None = 欠けた値)
fn category(v: f64) -> Option<i64> {
    if v.is_nan() || v < 0.0 || v.fract() != 0.0 || v > i64::MAX as f64 {
        None
    } else {
        Some(v as i64)
    }
}

/// 子: 0 以上は節の番号、負は葉 (!k が葉の番号)
#[derive(Clone, Debug, PartialEq)]
struct Node {
    feature: usize,
    split: Split,
    left: i32,
    right: i32,
}

#[derive(Clone, Debug, PartialEq)]
struct Tree {
    nodes: Vec<Node>,
    leaves: Vec<f64>,
}

impl Tree {
    fn predict(&self, row: &[f64]) -> f64 {
        if self.nodes.is_empty() {
            return self.leaves[0];
        }
        let mut at = 0i32;
        loop {
            let node = &self.nodes[at as usize];
            let next = if node.split.goes_left(row[node.feature]) { node.left } else { node.right };
            if next < 0 {
                return self.leaves[!next as usize];
            }
            at = next;
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
struct Model {
    features: usize,
    init: f64,
    trees: Vec<Tree>,
}

fn sigmoid(x: f64) -> f64 {
    1.0 / (1.0 + (-x).exp())
}

impl Model {
    fn raw(&self, row: &[f64]) -> f64 {
        self.init + self.trees.iter().map(|t| t.predict(row)).sum::<f64>()
    }

    fn to_text(&self) -> String {
        let mut out = format!("{TEXT_HEADER}\nfeatures {}\ninit {:?}\n", self.features, self.init);
        for t in &self.trees {
            out += &format!("tree {} {}\n", t.nodes.len(), t.leaves.len());
            for n in &t.nodes {
                match &n.split {
                    Split::Numeric { threshold, missing_left } => {
                        out += &format!("n {} {} {} num {:?} {}\n", n.feature, n.left, n.right, threshold,
                                        *missing_left as u8);
                    }
                    Split::Categorical { left } => {
                        let cats: Vec<String> = left.iter().map(|c| c.to_string()).collect();
                        out += &format!("n {} {} {} cat {}\n", n.feature, n.left, n.right, cats.join(","));
                    }
                }
            }
            let leaves: Vec<String> = t.leaves.iter().map(|v| format!("{v:?}")).collect();
            out += &format!("l {}\n", leaves.join(" "));
        }
        out
    }

    fn from_text(text: &str) -> Result<Model, String> {
        let mut lines = text.lines();
        if lines.next() != Some(TEXT_HEADER) {
            return Err("not a model of this version".into());
        }
        let value = |line: Option<&str>, key: &str| -> Result<String, String> {
            let line = line.ok_or("the model is cut off")?;
            line.strip_prefix(key).map(|s| s.trim().to_string()).ok_or(format!("expected {key}"))
        };
        let features: usize = value(lines.next(), "features")?.parse().map_err(|_| "bad features")?;
        let init: f64 = value(lines.next(), "init")?.parse().map_err(|_| "bad init")?;
        let mut trees = Vec::new();
        while let Some(line) = lines.next() {
            let head: Vec<&str> = line.split_whitespace().collect();
            if head.len() != 3 || head[0] != "tree" {
                return Err(format!("expected a tree: {line}"));
            }
            let (count, leaf_count): (usize, usize) =
                (head[1].parse().map_err(|_| "bad tree")?, head[2].parse().map_err(|_| "bad tree")?);
            let mut nodes = Vec::with_capacity(count);
            for _ in 0..count {
                let line = lines.next().ok_or("the model is cut off")?;
                let p: Vec<&str> = line.split_whitespace().collect();
                if p.len() < 5 || p[0] != "n" {
                    return Err(format!("expected a node: {line}"));
                }
                let feature: usize = p[1].parse().map_err(|_| "bad node")?;
                let (left, right): (i32, i32) = (p[2].parse().map_err(|_| "bad node")?,
                                                 p[3].parse().map_err(|_| "bad node")?);
                let split = match p[4] {
                    "num" if p.len() == 7 => Split::Numeric {
                        threshold: p[5].parse().map_err(|_| "bad threshold")?,
                        missing_left: p[6] == "1",
                    },
                    "cat" => Split::Categorical {
                        left: if p.len() > 5 {
                            p[5].split(',').map(|c| c.parse::<i64>().map_err(|_| "bad category"))
                                .collect::<Result<_, _>>()?
                        } else {
                            Vec::new()
                        },
                    },
                    _ => return Err(format!("bad split: {line}")),
                };
                if feature >= features {
                    return Err("feature out of range".into());
                }
                nodes.push(Node { feature, split, left, right });
            }
            let leaves: Vec<f64> = value(lines.next(), "l")?
                .split_whitespace()
                .map(|v| v.parse::<f64>().map_err(|_| "bad leaf".to_string()))
                .collect::<Result<_, _>>()?;
            if leaves.len() != leaf_count || leaves.is_empty() {
                return Err("bad leaf count".into());
            }
            for n in &nodes {
                for child in [n.left, n.right] {
                    let ok = if child < 0 { (!child as usize) < leaves.len() } else { (child as usize) < nodes.len() };
                    if !ok {
                        return Err("child out of range".into());
                    }
                }
            }
            trees.push(Tree { nodes, leaves });
        }
        Ok(Model { features, init, trees })
    }
}

// ---- 学習 -------------------------------------------------------------------

/// 特徴 1 つの区切り: 数値は区切りの値 (bounds[i] 以下が区切り i まで)、カテゴリは値の一覧。最後の区切りは欠けた値
enum Binner {
    Numeric { bounds: Vec<f64> },
    Categorical { cats: Vec<i64> },
}

impl Binner {
    fn numeric(values: &[f64]) -> Binner {
        let mut sorted: Vec<f64> = values.iter().copied().filter(|v| !v.is_nan()).collect();
        sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let mut distinct = sorted.clone();
        distinct.dedup();
        let mut bounds = Vec::new();
        if distinct.len() <= MAX_BINS {
            for w in distinct.windows(2) {
                bounds.push(w[0] + (w[1] - w[0]) / 2.0);
            }
        } else {
            let n = sorted.len();
            for k in 1..MAX_BINS {
                let i = k * n / MAX_BINS;
                if i == 0 || i >= n || sorted[i - 1] == sorted[i] {
                    continue;
                }
                bounds.push(sorted[i - 1] + (sorted[i] - sorted[i - 1]) / 2.0);
            }
            bounds.dedup();
        }
        Binner::Numeric { bounds }
    }

    fn categorical(values: &[f64]) -> Binner {
        let mut cats: Vec<i64> = values.iter().filter_map(|&v| category(v)).collect();
        cats.sort_unstable();
        cats.dedup();
        Binner::Categorical { cats }
    }

    /// 区切りの数 (欠けた値の区切りを含む)
    fn bins(&self) -> usize {
        match self {
            Binner::Numeric { bounds } => bounds.len() + 2,
            Binner::Categorical { cats } => cats.len() + 1,
        }
    }

    fn bin(&self, v: f64) -> usize {
        match self {
            Binner::Numeric { bounds } => {
                if v.is_nan() {
                    bounds.len() + 1
                } else {
                    bounds.partition_point(|&b| b < v)
                }
            }
            Binner::Categorical { cats } => match category(v) {
                Some(c) => cats.binary_search(&c).unwrap_or(cats.len()),
                None => cats.len(),
            },
        }
    }
}

/// 分け方の候補 (区切りで表す)
#[derive(Clone)]
enum BinSplit {
    Numeric { bin: usize, missing_left: bool },
    Categorical { bins: Vec<usize> },
}

#[derive(Clone)]
struct Candidate {
    gain: f64,
    feature: usize,
    split: BinSplit,
}

/// 葉の (勾配, ヘシアン, 行の数) を区切りごとに
#[derive(Clone)]
struct Histogram {
    g: Vec<f64>,
    h: Vec<f64>,
    n: Vec<u32>,
}

struct Data<'a> {
    rows: usize,
    binners: Vec<Binner>,
    offsets: Vec<usize>, // 特徴ごとのヒストグラムの先頭
    bins: Vec<Vec<u16>>, // 特徴ごと・行ごとの区切り
    x: &'a [f64],
    m: usize,
}

struct Leaf {
    rows: Vec<u32>,
    hist: Histogram,
    g: f64,
    h: f64,
    best: Option<Candidate>,
    slot: Option<(usize, bool)>, // 親の節と、その左か
}

fn leaf_score(g: f64, h: f64, lambda: f64) -> f64 {
    g * g / (h + lambda)
}

impl Data<'_> {
    fn histogram(&self, rows: &[u32], grad: &[f64], hess: &[f64], features: &[usize]) -> Histogram {
        let total = *self.offsets.last().unwrap();
        let mut hist = Histogram { g: vec![0.0; total], h: vec![0.0; total], n: vec![0; total] };
        for &f in features {
            let (bins, base) = (&self.bins[f], self.offsets[f]);
            for &r in rows {
                let k = base + bins[r as usize] as usize;
                hist.g[k] += grad[r as usize];
                hist.h[k] += hess[r as usize];
                hist.n[k] += 1;
            }
        }
        hist
    }

    fn best_split(&self, leaf: &Leaf, features: &[usize], p: &Params) -> Option<Candidate> {
        let (g_all, h_all, n_all) = (leaf.g, leaf.h, leaf.rows.len());
        let min_n = p.min_data_in_leaf;
        if n_all < 2 * min_n.max(1) {
            return None;
        }
        let mut best: Option<Candidate> = None;
        let mut consider = |gain: f64, feature: usize, split: BinSplit| {
            if gain > MIN_GAIN && best.as_ref().is_none_or(|b| gain > b.gain) {
                best = Some(Candidate { gain, feature, split });
            }
        };
        let ok = |gl: f64, hl: f64, nl: usize| {
            nl >= min_n && n_all - nl >= min_n && hl >= MIN_SUM_HESSIAN && h_all - hl >= MIN_SUM_HESSIAN
                && gl.is_finite()
        };
        for &f in features {
            let base = self.offsets[f];
            match &self.binners[f] {
                Binner::Numeric { bounds } => {
                    let lambda = p.lambda_l2;
                    let parent = leaf_score(g_all, h_all, lambda);
                    let miss = base + bounds.len() + 1;
                    let (gm, hm, nm) = (leaf.hist.g[miss], leaf.hist.h[miss], leaf.hist.n[miss] as usize);
                    for missing_left in [false, true] {
                        if missing_left && nm == 0 {
                            continue;
                        }
                        let (mut gl, mut hl, mut nl) = if missing_left { (gm, hm, nm) } else { (0.0, 0.0, 0) };
                        for s in 0..bounds.len() {
                            gl += leaf.hist.g[base + s];
                            hl += leaf.hist.h[base + s];
                            nl += leaf.hist.n[base + s] as usize;
                            if !ok(gl, hl, nl) {
                                continue;
                            }
                            let gain = leaf_score(gl, hl, lambda) + leaf_score(g_all - gl, h_all - hl, lambda) - parent;
                            consider(gain, f, BinSplit::Numeric { bin: s, missing_left });
                        }
                    }
                }
                Binner::Categorical { cats } => {
                    let lambda = p.lambda_l2 + CAT_L2;
                    let parent = leaf_score(g_all, h_all, lambda);
                    let used: Vec<usize> = (0..cats.len()).filter(|&c| leaf.hist.n[base + c] > 0).collect();
                    if used.len() < 2 {
                        continue;
                    }
                    let stats = |bins: &[usize]| {
                        bins.iter().fold((0.0, 0.0, 0usize), |(g, h, n), &c| {
                            (g + leaf.hist.g[base + c], h + leaf.hist.h[base + c], n + leaf.hist.n[base + c] as usize)
                        })
                    };
                    let mut try_left = |bins: Vec<usize>| {
                        let (gl, hl, nl) = stats(&bins);
                        if ok(gl, hl, nl) {
                            let gain = leaf_score(gl, hl, lambda) + leaf_score(g_all - gl, h_all - hl, lambda) - parent;
                            consider(gain, f, BinSplit::Categorical { bins });
                        }
                    };
                    if used.len() <= MAX_CAT_TO_ONEHOT {
                        for &c in &used {
                            try_left(vec![c]);
                        }
                    } else {
                        let mut order = used.clone();
                        let ratio = |c: usize| leaf.hist.g[base + c] / (leaf.hist.h[base + c] + CAT_SMOOTH);
                        order.sort_by(|&a, &b| ratio(a).partial_cmp(&ratio(b)).unwrap().then(a.cmp(&b)));
                        let limit = MAX_CAT_THRESHOLD.min(order.len() - 1);
                        for k in 1..=limit {
                            try_left(order[..k].to_vec());
                            try_left(order[order.len() - k..].to_vec());
                        }
                    }
                }
            }
        }
        best
    }

    fn goes_left(&self, split: &BinSplit, feature: usize, row: usize) -> bool {
        let b = self.bins[feature][row] as usize;
        match split {
            BinSplit::Numeric { bin, missing_left } => match &self.binners[feature] {
                Binner::Numeric { bounds } if b == bounds.len() + 1 => *missing_left,
                _ => b <= *bin,
            },
            BinSplit::Categorical { bins } => bins.contains(&b),
        }
    }

    /// 区切りの分け方を、元の値で木をたどる分け方にする
    fn split_of(&self, feature: usize, split: &BinSplit) -> Split {
        match (&self.binners[feature], split) {
            (Binner::Numeric { bounds }, BinSplit::Numeric { bin, missing_left }) => {
                Split::Numeric { threshold: bounds[*bin], missing_left: *missing_left }
            }
            (Binner::Categorical { cats }, BinSplit::Categorical { bins }) => {
                let mut left: Vec<i64> = bins.iter().map(|&b| cats[b]).collect();
                left.sort_unstable();
                Split::Categorical { left }
            }
            _ => unreachable!("split kind matches the feature kind"),
        }
    }

    fn row(&self, r: usize) -> &[f64] {
        &self.x[r * self.m..(r + 1) * self.m]
    }
}

fn subtract(parent: &Histogram, child: &Histogram) -> Histogram {
    Histogram {
        g: parent.g.iter().zip(&child.g).map(|(a, b)| a - b).collect(),
        h: parent.h.iter().zip(&child.h).map(|(a, b)| a - b).collect(),
        n: parent.n.iter().zip(&child.n).map(|(a, b)| a - b).collect(),
    }
}

/// 木 1 本を葉ごとに伸ばす
fn grow_tree(data: &Data, rows: Vec<u32>, grad: &[f64], hess: &[f64], features: &[usize], p: &Params) -> Tree {
    let (g, h) = rows.iter().fold((0.0, 0.0), |(g, h), &r| (g + grad[r as usize], h + hess[r as usize]));
    let hist = data.histogram(&rows, grad, hess, features);
    let mut root = Leaf { rows, hist, g, h, best: None, slot: None };
    root.best = data.best_split(&root, features, p);
    let mut leaves = vec![root];
    let mut nodes: Vec<Node> = Vec::new();
    while leaves.len() < p.num_leaves.max(1) {
        let Some(k) = (0..leaves.len())
            .filter(|&k| leaves[k].best.is_some())
            .max_by(|&a, &b| {
                let (ga, gb) = (leaves[a].best.as_ref().unwrap().gain, leaves[b].best.as_ref().unwrap().gain);
                ga.partial_cmp(&gb).unwrap().then(b.cmp(&a))
            })
        else {
            break;
        };
        let leaf = std::mem::replace(&mut leaves[k], Leaf {
            rows: Vec::new(), hist: Histogram { g: Vec::new(), h: Vec::new(), n: Vec::new() }, g: 0.0, h: 0.0,
            best: None, slot: None,
        });
        let cand = leaf.best.clone().unwrap();
        let (mut left_rows, mut right_rows) = (Vec::new(), Vec::new());
        for &r in &leaf.rows {
            if data.goes_left(&cand.split, cand.feature, r as usize) {
                left_rows.push(r);
            } else {
                right_rows.push(r);
            }
        }
        let new_leaf = leaves.len();
        let node = nodes.len();
        nodes.push(Node { feature: cand.feature, split: data.split_of(cand.feature, &cand.split), left: !(k as i32),
                          right: !(new_leaf as i32) });
        if let Some((parent, is_left)) = leaf.slot {
            if is_left {
                nodes[parent].left = node as i32;
            } else {
                nodes[parent].right = node as i32;
            }
        }
        let small_is_left = left_rows.len() <= right_rows.len();
        let small_rows = if small_is_left { &left_rows } else { &right_rows };
        let small_hist = data.histogram(small_rows, grad, hess, features);
        let large_hist = subtract(&leaf.hist, &small_hist);
        let (left_hist, right_hist) = if small_is_left { (small_hist, large_hist) } else { (large_hist, small_hist) };
        let sums = |rows: &[u32]| rows.iter().fold((0.0, 0.0), |(g, h), &r| (g + grad[r as usize], h + hess[r as usize]));
        let ((lg, lh), (rg, rh)) = (sums(&left_rows), sums(&right_rows));
        let mut left = Leaf { rows: left_rows, hist: left_hist, g: lg, h: lh, best: None, slot: Some((node, true)) };
        let mut right = Leaf { rows: right_rows, hist: right_hist, g: rg, h: rh, best: None, slot: Some((node, false)) };
        left.best = data.best_split(&left, features, p);
        right.best = data.best_split(&right, features, p);
        leaves[k] = left;
        leaves.push(right);
    }
    let values = leaves.iter().map(|l| -l.g / (l.h + p.lambda_l2) * p.learning_rate).collect();
    Tree { nodes, leaves: values }
}

fn train(x: &[f64], n: usize, m: usize, y: &[f64], w: &[f64], categorical: &[usize], p: &Params) -> Model {
    let binners: Vec<Binner> = (0..m)
        .map(|f| {
            let column: Vec<f64> = (0..n).map(|r| x[r * m + f]).collect();
            if categorical.contains(&f) { Binner::categorical(&column) } else { Binner::numeric(&column) }
        })
        .collect();
    let mut offsets = vec![0usize];
    for b in &binners {
        offsets.push(offsets.last().unwrap() + b.bins());
    }
    let bins: Vec<Vec<u16>> =
        (0..m).map(|f| (0..n).map(|r| binners[f].bin(x[r * m + f]) as u16).collect()).collect();
    let data = Data { rows: n, binners, offsets, bins, x, m };

    let (sw, swy) = (0..n).fold((0.0, 0.0), |(a, b), r| (a + w[r], b + w[r] * y[r]));
    let init = if sw > 0.0 {
        let p = (swy / sw).clamp(1e-15, 1.0 - 1e-15);
        (p / (1.0 - p)).ln()
    } else {
        0.0
    };
    let mut model = Model { features: m, init, trees: Vec::new() };
    if n == 0 || m == 0 {
        return model;
    }
    let mut rng = Rng(p.seed);
    let mut score = vec![init; n];
    let (mut grad, mut hess) = (vec![0.0; n], vec![0.0; n]);
    let mut bag: Vec<u32> = (0..n as u32).collect();
    let feature_count = ((m as f64 * p.feature_fraction).round() as usize).clamp(1, m);
    for round in 0..p.rounds {
        for r in 0..n {
            let prob = sigmoid(score[r]);
            grad[r] = (prob - y[r]) * w[r];
            hess[r] = (prob * (1.0 - prob)).max(1e-16) * w[r];
        }
        if p.bagging_freq > 0 && p.bagging_fraction < 1.0 && round % p.bagging_freq == 0 {
            bag = (0..data.rows as u32).filter(|_| rng.uniform() < p.bagging_fraction).collect();
            if bag.is_empty() {
                bag = (0..data.rows as u32).collect();
            }
        }
        let mut order: Vec<usize> = (0..m).collect();
        for i in 0..feature_count {
            let j = i + rng.below(m - i);
            order.swap(i, j);
        }
        let mut features = order[..feature_count].to_vec();
        features.sort_unstable();
        let tree = grow_tree(&data, bag.clone(), &grad, &hess, &features, p);
        for (r, s) in score.iter_mut().enumerate() {
            *s += tree.predict(data.row(r));
        }
        model.trees.push(tree);
    }
    model
}

// ---- Python から使う形 -------------------------------------------------------------------

/// 学習したモデル (model.py の LearnedModel が持つ)
#[pyclass(module = "ts_smartcut_native", name = "GbdtModel")]
pub struct GbdtModel {
    model: Model,
}

#[pymethods]
impl GbdtModel {
    /// x は float64 の (n, m) (欠けた値は NaN)、y は 0/1・weight は重み (float64 の n 個)、categorical は
    /// カテゴリの特徴の番号
    #[staticmethod]
    #[pyo3(signature = (x, n, m, y, weight, categorical, rounds, learning_rate, num_leaves, min_data_in_leaf,
                        lambda_l2, feature_fraction, bagging_fraction, bagging_freq, seed))]
    #[allow(clippy::too_many_arguments)]
    fn train(py: Python<'_>, x: &[u8], n: usize, m: usize, y: &[u8], weight: &[u8], categorical: Vec<usize>,
             rounds: usize, learning_rate: f64, num_leaves: usize, min_data_in_leaf: usize, lambda_l2: f64,
             feature_fraction: f64, bagging_fraction: f64, bagging_freq: usize, seed: u64) -> PyResult<Self> {
        let (x, y, w) = (bytes_f64(x), bytes_f64(y), bytes_f64(weight));
        if x.len() != n * m || y.len() != n || w.len() != n {
            return Err(PyValueError::new_err("the sizes of x, y and weight do not match"));
        }
        if categorical.iter().any(|&f| f >= m) {
            return Err(PyValueError::new_err("a categorical feature is out of range"));
        }
        let p = Params { rounds, learning_rate, num_leaves, min_data_in_leaf, lambda_l2, feature_fraction,
                         bagging_fraction, bagging_freq, seed };
        let model = py.detach(|| train(&x, n, m, &y, &w, &categorical, &p));
        Ok(GbdtModel { model })
    }

    #[staticmethod]
    fn from_text(text: &str) -> PyResult<Self> {
        Model::from_text(text).map(|model| GbdtModel { model }).map_err(PyValueError::new_err)
    }

    fn to_text(&self) -> String {
        self.model.to_text()
    }

    /// 切る確率 (float64 の n 個)。x は float64 の (n, m)
    fn predict<'py>(&self, py: Python<'py>, x: &[u8], n: usize, m: usize) -> PyResult<Bound<'py, PyBytes>> {
        let x = bytes_f64(x);
        if m != self.model.features || x.len() != n * m {
            return Err(PyValueError::new_err("the size of x does not match the model"));
        }
        let out: Vec<f64> = (0..n).map(|r| sigmoid(self.model.raw(&x[r * m..(r + 1) * m]))).collect();
        Ok(to_bytes(py, &out, |v: f64| v.to_le_bytes().to_vec()))
    }

    #[getter]
    fn num_features(&self) -> usize {
        self.model.features
    }

    #[getter]
    fn num_trees(&self) -> usize {
        self.model.trees.len()
    }
}
