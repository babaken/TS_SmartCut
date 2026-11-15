//! しきい値の自動補正 (ts_smartcut/autocut/tuning.py) の評価 1 回分: 録画ごとに局の値を測り、その録画を除いた
//! 局の値で案を作って正解と比べた合計。決まり (境目の候補・区間の判定・案・比較) は rules.rs (案づくりと同じ)。

use pyo3::prelude::*;
use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex};

use crate::rules::{Boundary, Recording, RuleRecording, Score, ScoreTuple, Thresholds, TruthSpans, compare,
                   profile_from_measures};

/// 補正に使う録画 1 本 (tuning.TuneSample の特徴と正解)。特徴は案づくりと同じ RuleRecording を共有する。
/// 境目の候補は値の組ごとに覚えておく
#[pyclass(module = "ts_smartcut_native", frozen)]
pub struct TuneSample {
    recording_id: i64,
    station_key: String,
    recording: Arc<Recording>,
    truth: Vec<(i64, i64, bool)>,
    truth_spans: TruthSpans,
    window: Option<(f64, f64)>,
    boundaries: Mutex<HashMap<[u64; 5], Arc<Vec<Boundary>>>>,
}

#[pymethods]
impl TuneSample {
    #[new]
    #[pyo3(signature = (recording_id, station_key, recording, truth, window))]
    fn new(recording_id: i64, station_key: String, recording: &RuleRecording, truth: Vec<(i64, i64, bool)>,
           window: Option<(f64, f64)>) -> TuneSample {
        let truth_spans = TruthSpans::new(&truth, recording.inner.frame_count);
        TuneSample {
            recording_id,
            station_key,
            recording: recording.inner.clone(),
            truth,
            truth_spans,
            window,
            boundaries: Mutex::new(HashMap::new()),
        }
    }
}

impl TuneSample {
    fn boundaries_for(&self, t: &Thresholds) -> Arc<Vec<Boundary>> {
        let key = t.candidate_key();
        if let Some(found) = self.boundaries.lock().unwrap().get(&key) {
            return found.clone();
        }
        let found = Arc::new(self.recording.find_boundaries(t));
        self.boundaries.lock().unwrap().insert(key, found.clone());
        found
    }
}

/// tuning.evaluate: samples の案を作って正解と比べた合計。thresholds は局の鍵 → THRESHOLD_NAMES の順の値。
/// score_ids を渡すと、その録画だけを数える (局の値はほかの録画からも測る)。
#[pyfunction]
#[pyo3(signature = (samples, thresholds, score_ids=None))]
pub fn evaluate_samples(
    py: Python<'_>,
    samples: Vec<Py<TuneSample>>,
    thresholds: HashMap<String, Vec<f64>>,
    score_ids: Option<HashSet<i64>>,
) -> PyResult<ScoreTuple> {
    let mut table: HashMap<String, Thresholds> = HashMap::new();
    for (key, values) in &thresholds {
        table.insert(key.clone(), Thresholds::from_values(values)?);
    }
    for s in &samples {
        if !table.contains_key(&s.get().station_key) {
            return Err(pyo3::exceptions::PyKeyError::new_err(s.get().station_key.clone()));
        }
    }
    let total = py.detach(|| {
        // 局ごと (最初に出てきた順。Python の dict と同じ) に分ける
        let mut order: Vec<&str> = Vec::new();
        let mut groups: HashMap<&str, Vec<&TuneSample>> = HashMap::new();
        for s in &samples {
            let s = s.get();
            groups
                .entry(&s.station_key)
                .or_insert_with(|| {
                    order.push(&s.station_key);
                    Vec::new()
                })
                .push(s);
        }
        let mut total = Score::default();
        for key in order {
            let group = &groups[key];
            let t = table[key];
            let measures: Vec<(i64, Vec<i64>, Vec<bool>)> = group
                .iter()
                .map(|s| {
                    let boundaries = s.boundaries_for(&t);
                    let rec = &s.recording;
                    (s.recording_id, rec.measure_end_offsets(&s.truth, &boundaries),
                     rec.measure_short_between(&s.truth, s.window, &boundaries, &t))
                })
                .collect();
            // Python の dict は同じ録画を 1 つにまとめる (後の値が残り、位置は最初のまま)
            let mut unique: Vec<(i64, Vec<i64>, Vec<bool>)> = Vec::new();
            for m in measures {
                match unique.iter_mut().find(|u| u.0 == m.0) {
                    Some(u) => *u = m,
                    None => unique.push(m),
                }
            }
            for s in group {
                if score_ids.as_ref().is_some_and(|ids| !ids.contains(&s.recording_id)) {
                    continue;
                }
                let profile = profile_from_measures(&unique, Some(s.recording_id));
                let boundaries = s.boundaries_for(&t);
                let proposals = s.recording.propose(s.window, &profile, &boundaries, &t);
                total.add(&compare(&s.truth_spans, &proposals, s.recording.frame_count, s.recording.fps));
            }
        }
        total
    });
    Ok(total.tuple())
}
