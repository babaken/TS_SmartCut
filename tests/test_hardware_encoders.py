"""ハードウェアエンコーダー判定結果の保存・読み込み (core/hardware_encoders.py)。

判定は初回起動時の1回だけ行い、以降は保存した結果を使う。conftest.pyで
設定ディレクトリはテスト用の一時ディレクトリに向いている。
"""

import json

import pytest

from ts_smartcut.core import hardware_encoders as module
from ts_smartcut.ffmpeg.gpu import HardwareDetection, HardwareEncoder
from ts_smartcut.models import AppSettings


@pytest.fixture
def hardware(monkeypatch):
    """このPCのGPU・CPU構成(hardware_fingerprint)。テストから差し替える。"""
    current = {"id": "amd-apu|amd-cpu"}
    monkeypatch.setattr(module, "hardware_fingerprint", lambda: current["id"])
    return current


@pytest.fixture
def ffmpeg_build(monkeypatch):
    """入っている ffmpeg のビルド(DependencyChecker.ffmpeg_build_id)。テストから差し替える。"""
    current = {"id": "n8.1.3 (0123456789ab)"}
    monkeypatch.setattr(module.DependencyChecker, "ffmpeg_build_id", staticmethod(lambda path=None: current["id"]))
    return current


@pytest.fixture
def counting_detector(monkeypatch, hardware, ffmpeg_build):
    calls = []

    def fake_detect(**kwargs):
        calls.append(kwargs)
        return HardwareDetection(
            HardwareEncoder(
                "vaapi", "amd", False, "/dev/dri/renderD128",
                {"h264": {"progressive": True, "interlaced": False}},
            ),
            hardware_id=hardware["id"],
            ffmpeg_build=ffmpeg_build["id"],
        )

    monkeypatch.setattr(module, "detect_hardware_encoders", fake_detect)
    module.clear_cache()
    module.detection_path().unlink(missing_ok=True)
    yield calls
    module.clear_cache()
    module.detection_path().unlink(missing_ok=True)


def test_detects_once_on_first_launch_and_saves(counting_detector):
    first = module.get_hardware_encoders(AppSettings())
    assert len(counting_detector) == 1
    saved = json.loads(module.detection_path().read_text(encoding="utf-8"))
    assert saved["selected"]["api"] == "vaapi"

    # 同じ起動中は判定し直さない
    assert module.get_hardware_encoders(AppSettings()) is first
    assert len(counting_detector) == 1


def test_later_launch_reads_saved_result_without_detecting(counting_detector):
    module.get_hardware_encoders(AppSettings())
    module.clear_cache()  # アプリを再起動したのと同じ状態

    detection = module.get_hardware_encoders(AppSettings())
    assert len(counting_detector) == 1
    assert detection.selected.vendor == "amd"


def test_force_redetects(counting_detector):
    module.get_hardware_encoders(AppSettings())
    module.get_hardware_encoders(AppSettings(), force=True)
    assert len(counting_detector) == 2


def test_corrupt_saved_result_is_redetected(counting_detector):
    module.detection_path().parent.mkdir(parents=True, exist_ok=True)
    module.detection_path().write_text("{broken", encoding="utf-8")
    module.get_hardware_encoders(AppSettings())
    assert len(counting_detector) == 1


def test_changed_hardware_is_redetected(counting_detector, hardware):
    module.get_hardware_encoders(AppSettings())
    module.clear_cache()
    hardware["id"] = "nvidia-rtx|amd-apu|amd-cpu"  # ビデオカードを増設した
    module.get_hardware_encoders(AppSettings())
    assert len(counting_detector) == 2

    # 新しい構成で保存し直したので、次の起動では判定しない
    module.clear_cache()
    module.get_hardware_encoders(AppSettings())
    assert len(counting_detector) == 2


def test_result_from_older_detection_method_is_redetected(counting_detector):
    # 判定方法の版が古い結果(最優先のGPUしか調べていなかった頃)は使わない
    module.get_hardware_encoders(AppSettings())
    saved = json.loads(module.detection_path().read_text(encoding="utf-8"))
    del saved["version"]
    module.detection_path().write_text(json.dumps(saved), encoding="utf-8")
    module.clear_cache()
    module.get_hardware_encoders(AppSettings())
    assert len(counting_detector) == 2


def test_changed_ffmpeg_is_redetected(counting_detector, ffmpeg_build):
    # GPU の部品の有無や動きは ffmpeg のビルドで違う。入れ替えたら判定し直す(2026-11-17、利用者の決定)
    module.get_hardware_encoders(AppSettings())
    module.clear_cache()
    ffmpeg_build["id"] = "n7.1.5 (fedcba987654)"
    module.get_hardware_encoders(AppSettings())
    assert len(counting_detector) == 2
    saved = json.loads(module.detection_path().read_text(encoding="utf-8"))
    assert saved["ffmpeg_build"] == "n7.1.5 (fedcba987654)"

    module.clear_cache()
    module.get_hardware_encoders(AppSettings())
    assert len(counting_detector) == 2


def test_unreadable_ffmpeg_does_not_redetect(counting_detector, ffmpeg_build):
    # 版が読めない時(時間切れなど)は、起動のたびに判定し直さない
    module.get_hardware_encoders(AppSettings())
    module.clear_cache()
    ffmpeg_build["id"] = None
    module.get_hardware_encoders(AppSettings())
    assert len(counting_detector) == 1
