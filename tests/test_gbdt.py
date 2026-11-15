"""学習のモデル (Rust の部品の GbdtModel、native/src/gbdt.rs、段階 ④ 2026-10-30) のテスト。

合成のデータで、数値の特徴・欠けた値・カテゴリの特徴・行の重みを学ぶこと、同じ入力なら毎回同じモデルになること、
文字列に保存して読み直しても同じ予測になることを確かめる。LightGBM が入っている PC では、同じ設定の LightGBM と
確認用のデータでの良さ (対数損失) が大きく違わないことも見る (LightGBM は 2026-10-30 に依存から外した)。
区間の判定としての中身 (番宣を切る・ルールより良い時だけ使う) は test_autocut_model.py。
"""

import math

import numpy as np
import pytest

from ts_smartcut.autocut import model as learning
from ts_smartcut.autocut.native import native_module

GbdtModel = native_module().GbdtModel
PARAMS = dict(learning._PARAMS)


def _train(x, y, w=None, categorical=(), **override):
    x = np.ascontiguousarray(x, np.float64)
    w = np.ones(len(y)) if w is None else np.asarray(w, np.float64)
    params = {**PARAMS, **override}
    return GbdtModel.train(x.tobytes(), x.shape[0], x.shape[1], np.asarray(y, np.float64).tobytes(), w.tobytes(),
                           list(categorical), **params)


def _predict(model, x):
    x = np.ascontiguousarray(x, np.float64)
    return np.frombuffer(model.predict(x.tobytes(), x.shape[0], x.shape[1]), "<f8")


def _logloss(p, y):
    p = np.clip(p, 1e-12, 1 - 1e-12)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def test_numeric_features_with_an_interaction_are_learned():
    rng = np.random.default_rng(0)
    x = rng.random((2000, 4))
    y = ((x[:, 0] > 0.3) & (x[:, 1] < 0.6)).astype(float)
    model = _train(x, y)
    test = rng.random((500, 4))
    want = (test[:, 0] > 0.3) & (test[:, 1] < 0.6)
    assert ((_predict(model, test) >= 0.5) == want).mean() > 0.95
    assert model.num_trees == PARAMS["rounds"] and model.num_features == 4


def test_missing_values_go_to_the_better_side():
    rng = np.random.default_rng(1)
    x = rng.random((1500, 2))
    y = (x[:, 0] > 0.5).astype(float)
    missing = rng.random(1500) < 0.3
    x[missing, 0] = np.nan
    y[missing] = 1.0  # 欠けた値は「切る」
    model = _train(x, y)
    assert _predict(model, np.array([[np.nan, 0.5], [0.1, 0.5], [0.9, 0.5]])).round().tolist() == [1.0, 0.0, 1.0]


def test_categorical_features_split_by_category():
    rng = np.random.default_rng(2)
    category = rng.integers(0, 12, 3000).astype(float)
    noise = rng.random(3000)
    x = np.column_stack([category, noise])
    y = np.isin(category, [1, 4, 7, 10]).astype(float)
    model = _train(x, y, categorical=[0])
    cats = np.arange(12, dtype=float)
    p = _predict(model, np.column_stack([cats, np.full(12, 0.5)]))
    assert ((p >= 0.5) == np.isin(cats, [1, 4, 7, 10])).all()
    # 学習で見ていないカテゴリ・欠けた値でも予測できる (右へ行く)
    assert np.isfinite(_predict(model, np.array([[99.0, 0.5], [np.nan, 0.5], [-1.0, 0.5]]))).all()


def test_row_weights_decide_between_conflicting_labels():
    x = np.zeros((400, 1))
    y = np.array([1.0] * 200 + [0.0] * 200)
    heavy_cut = np.array([10.0] * 200 + [1.0] * 200)
    assert _predict(_train(x, y, heavy_cut), np.zeros((1, 1)))[0] > 0.85
    assert _predict(_train(x, y, heavy_cut[::-1]), np.zeros((1, 1)))[0] < 0.15


def test_the_same_input_gives_the_same_model_and_text_round_trips():
    rng = np.random.default_rng(3)
    x = rng.random((800, 5))
    x[rng.random(x.shape) < 0.1] = np.nan
    x[:, 4] = rng.integers(0, 6, 800)
    y = (np.nan_to_num(x[:, 0]) + rng.normal(0, 0.2, 800) > 0.5).astype(float)
    a, b = _train(x, y, categorical=[4]), _train(x, y, categorical=[4])
    assert a.to_text() == b.to_text()
    again = GbdtModel.from_text(a.to_text())
    assert _predict(again, x).tobytes() == _predict(a, x).tobytes()
    assert _train(x, y, categorical=[4], seed=2).to_text() != a.to_text()  # 種が違えば行と特徴の選び方が違う


def test_bad_input_and_text_are_errors():
    with pytest.raises(ValueError):
        GbdtModel.train(b"\0" * 8, 2, 1, b"\0" * 16, b"\0" * 16, [], **PARAMS)  # x が足りない
    with pytest.raises(ValueError):
        GbdtModel.train(b"\0" * 16, 2, 1, b"\0" * 16, b"\0" * 16, [3], **PARAMS)  # カテゴリの番号が範囲外
    model = _train(np.random.default_rng(4).random((100, 2)), np.arange(100) % 2)
    with pytest.raises(ValueError):
        model.predict(b"\0" * 24, 1, 3)  # 特徴の数が違う
    for text in ("", "tree\nversion=v4\n", model.to_text().replace("features 2", "features 1"),
                 model.to_text()[:200]):
        with pytest.raises(ValueError):
            GbdtModel.from_text(text)


def test_no_rows_gives_a_constant_model():
    model = GbdtModel.train(b"", 0, 3, b"", b"", [], **PARAMS)
    assert model.num_trees == 0
    assert _predict(model, np.zeros((2, 3))).tolist() == [0.5, 0.5]


def _segment_like(rng, n):
    """区間の表に似たデータ: 長さ・15 秒とのずれ・ロゴ・無音・局 (カテゴリ)・欠けた値を混ぜる。"""
    seconds = rng.choice([15, 30, 60, 45, 120, 300, 600], n) + rng.normal(0, 1, n)
    err15 = np.abs(seconds - 15 * np.round(seconds / 15))
    logo = rng.random(n)
    logo[rng.random(n) < 0.1] = np.nan
    silent = rng.random(n)
    station = rng.integers(0, 8, n).astype(float)
    x = np.column_stack([seconds, err15, logo, silent, station])
    score = (seconds < 100) * 1.5 + (err15 < 0.5) * 1.0 - np.nan_to_num(logo, nan=0.5) * 2.0 + (station % 3 == 0) * 0.7
    y = (score + rng.normal(0, 0.7, n) > 0.6).astype(float)
    return x, y, np.clip(seconds, 1.0, learning.MAX_WEIGHT_SECONDS)


def test_quality_is_close_to_lightgbm_with_the_same_settings():
    lgb = pytest.importorskip("lightgbm")
    rng = np.random.default_rng(5)
    x, y, w = _segment_like(rng, 4000)
    test_x, test_y, _ = _segment_like(rng, 2000)
    ours = _logloss(_predict(_train(x, y, w, categorical=[4]), test_x), test_y)
    params = {"objective": "binary", "learning_rate": 0.05, "num_leaves": 15, "min_data_in_leaf": 20,
              "feature_fraction": 0.9, "bagging_fraction": 0.9, "bagging_freq": 1, "lambda_l2": 1.0, "seed": 1,
              "deterministic": True, "verbose": -1}
    booster = lgb.train(params, lgb.Dataset(x, y, weight=w, categorical_feature=[4]), PARAMS["rounds"])
    theirs = _logloss(booster.predict(test_x), test_y)
    assert math.isfinite(ours) and ours <= theirs + 0.02
