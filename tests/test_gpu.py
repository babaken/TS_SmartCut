"""ハードウェアエンコーダーの判定と選択 (ffmpeg/gpu.py)。"""

import platform

import pytest

from ts_smartcut.ffmpeg import gpu as gpu_module
from ts_smartcut.ffmpeg.gpu import (
    HardwareDetection,
    HardwareEncoder,
    choose_encoder,
    choose_hardware,
    detect_hardware_encoders,
    hardware_fingerprint,
    encoder_gpu_type,
    find_vaapi_device,
    hw_filter,
    hw_input_args,
    is_encoder_available,
    list_available_encoders,
    probe_encoder_functional,
    probe_interlaced_encoding,
    quality_args,
)


def _encoder(api="vaapi", vendor="amd", discrete=False, device="/dev/dri/renderD128", **caps):
    capabilities = {
        "h264": {"progressive": caps.get("h264_p", False), "interlaced": caps.get("h264_i", False)},
        "mpeg2": {"progressive": caps.get("mpeg2_p", False), "interlaced": caps.get("mpeg2_i", False)},
    }
    return HardwareEncoder(api, vendor, discrete, device, capabilities)


# ---- 選択 ------------------------------------------------------------------

def test_choose_encoder_without_hardware_uses_software():
    assert choose_encoder(None, "h264", interlaced=False) == "libx264"
    assert choose_encoder(HardwareDetection(), "mpeg2", interlaced=True) == "mpeg2video"


def test_choose_encoder_uses_gpu_only_for_what_it_can_do():
    # この開発PC(AMD APU, VAAPI)と同じ能力: H.264のプログレッシブのみ
    detection = HardwareDetection(_encoder(h264_p=True))
    assert choose_encoder(detection, "h264", interlaced=False) == "h264_vaapi"
    assert choose_encoder(detection, "h264", interlaced=True) == "libx264"
    assert choose_encoder(detection, "mpeg2", interlaced=False) == "mpeg2video"


def test_choose_encoder_qsv_with_interlace_support():
    detection = HardwareDetection(_encoder("qsv", "intel", False, None, h264_p=True, h264_i=True))
    assert choose_encoder(detection, "h264", interlaced=True) == "h264_qsv"


def test_choose_encoder_falls_through_to_next_gpu_in_priority_order():
    # 利用者の要件: ビデオカード → CPU内蔵 → CPU(ソフトウェア)の順に、
    # 必要なことができるものを使う(例: NVENCはインターレース・MPEG-2不可 → QSV)
    nvenc = _encoder("nvenc", "nvidia", True, None, h264_p=True)
    qsv = _encoder("qsv", "intel", False, None, h264_p=True, h264_i=True, mpeg2_p=True)
    detection = HardwareDetection(nvenc, [nvenc, qsv])
    assert choose_encoder(detection, "h264", interlaced=False) == "h264_nvenc"
    assert choose_encoder(detection, "h264", interlaced=True) == "h264_qsv"
    assert choose_encoder(detection, "mpeg2", interlaced=False) == "mpeg2_qsv"
    assert choose_encoder(detection, "mpeg2", interlaced=True) == "mpeg2video"  # どちらも不可


def test_choose_hardware_gives_the_device_of_the_gpu_it_picked():
    # 同じVAAPIのGPUが2つ(AMDのビデオカード+APU)でも、使うGPUのデバイスを返す
    card = _encoder("vaapi", "amd", True, "/dev/dri/renderD128", h264_p=True)
    apu = _encoder("vaapi", "amd", False, "/dev/dri/renderD129", h264_p=True, mpeg2_p=True)
    detection = HardwareDetection(card, [card, apu])
    assert choose_hardware(detection, "h264", interlaced=False).device == "/dev/dri/renderD128"
    assert choose_hardware(detection, "mpeg2", interlaced=False).device == "/dev/dri/renderD129"
    assert choose_hardware(detection, "h264", interlaced=True) is None


def test_detection_round_trips_through_dict():
    detection = HardwareDetection(_encoder(h264_p=True), [_encoder(h264_p=True)])
    assert HardwareDetection.from_dict(detection.to_dict()) == detection


def test_describe_lists_capabilities():
    text = HardwareDetection(_encoder(h264_p=True)).describe()
    assert "AMD VAAPI（CPU内蔵）" in text
    assert "H.264: プログレッシブ ○ / インターレース ×" in text
    assert "ソフトウェアエンコーダー" in HardwareDetection().describe()


# ---- ビデオカード/CPU内蔵の見分けと優先順位 ------------------------------------------

def _fake_drm(tmp_path, devices):
    """/sys/class/drm/renderD* を模したディレクトリを作る。"""
    for index, (vendor, pci, vram) in enumerate(devices):
        pci_dir = tmp_path / "pci" / pci
        try:
            pci_dir.mkdir(parents=True)
        except OSError:
            # PCI位置(例: 0000:03:00.0)の":"はWindowsのファイル名に使えない。
            pytest.skip("PCI位置の名前のディレクトリを作れない環境(Windows)")
        (pci_dir / "vendor").write_text(vendor)
        if vram is not None:
            (pci_dir / "mem_info_vram_total").write_text(str(vram))
        node = tmp_path / "drm" / f"renderD{128 + index}"
        node.mkdir(parents=True)
        try:
            (node / "device").symlink_to(pci_dir)
        except OSError:
            # Windowsでは権限(開発者モード等)が無いとシンボリックリンクを作れない。
            # この判定はLinuxのsysfs用なので、作れない環境ではスキップする。
            pytest.skip("シンボリックリンクを作れない環境")
    return tmp_path / "drm"


@pytest.fixture
def every_encoder_works(monkeypatch):
    monkeypatch.setattr(gpu_module, "list_available_encoders", lambda ffmpeg_path="ffmpeg": [
        "h264_nvenc", "h264_amf", "h264_qsv", "h264_vaapi", "mpeg2_qsv", "mpeg2_vaapi"])
    monkeypatch.setattr(gpu_module, "probe_encoder_functional", lambda *a, **k: True)
    monkeypatch.setattr(gpu_module, "probe_interlaced_encoding", lambda *a, **k: False)


@pytest.fixture
def no_nvidia(monkeypatch, every_encoder_works):
    monkeypatch.setattr(gpu_module, "list_available_encoders", lambda ffmpeg_path="ffmpeg": [
        "h264_amf", "h264_qsv", "h264_vaapi", "mpeg2_qsv", "mpeg2_vaapi"])


GIB = 1024 ** 3


@pytest.mark.parametrize(
    "devices, cpu, expected",
    [
        # AMD APUのみ(この開発PC): CPU内蔵のAMDが選ばれる
        ([("0x1002", "0000:03:00.0", 2 * GIB)], "amd", ("amd", False)),
        # Intel CPU + AMDビデオカード: AMD(ビデオカード)がIntel内蔵より優先
        ([("0x8086", "0000:00:02.0", None), ("0x1002", "0000:01:00.0", 8 * GIB)], "intel", ("amd", True)),
        # AMD APU + AMDビデオカード: メモリの大きい方がビデオカード
        ([("0x1002", "0000:07:00.0", 2 * GIB), ("0x1002", "0000:01:00.0", 16 * GIB)], "amd", ("amd", True)),
        # Intel内蔵のみ(00:02.0): CPU内蔵のIntel(QSV)
        ([("0x8086", "0000:00:02.0", None)], "intel", ("intel", False)),
        # Intel内蔵 + Intel Arc: Arc(ビデオカード)
        ([("0x8086", "0000:00:02.0", None), ("0x8086", "0000:03:00.0", None)], "intel", ("intel", True)),
    ],
)
def test_linux_priority_video_card_then_integrated(tmp_path, no_nvidia, devices, cpu, expected):
    drm = _fake_drm(tmp_path, devices)
    detection = detect_hardware_encoders(os_name="Linux", cpu_vendor=cpu, sys_drm=drm)
    assert (detection.selected.vendor, detection.selected.discrete) == expected


def test_nvidia_is_preferred_as_video_card(tmp_path, every_encoder_works):
    drm = _fake_drm(tmp_path, [("0x1002", "0000:03:00.0", 2 * GIB)])  # AMD APU + NVIDIA
    detection = detect_hardware_encoders(os_name="Linux", cpu_vendor="amd", sys_drm=drm)
    assert detection.selected.vendor == "nvidia"


def test_windows_amd_on_intel_cpu_is_video_card(every_encoder_works):
    detection = detect_hardware_encoders(os_name="Windows", cpu_vendor="intel")
    order = [(c.api, c.discrete) for c in detection.candidates]
    assert order == [("nvenc", True), ("amf", True), ("qsv", False)]


def test_windows_amd_apu_is_integrated(no_nvidia):
    detection = detect_hardware_encoders(os_name="Windows", cpu_vendor="amd")
    assert (detection.selected.api, detection.selected.discrete) == ("amf", False)


def test_capabilities_are_probed_for_selected_gpu(tmp_path, no_nvidia):
    drm = _fake_drm(tmp_path, [("0x1002", "0000:03:00.0", 2 * GIB)])
    detection = detect_hardware_encoders(os_name="Linux", cpu_vendor="amd", sys_drm=drm)
    assert detection.selected.capabilities == {
        "h264": {"progressive": True, "interlaced": False},
        "mpeg2": {"progressive": True, "interlaced": False},
    }


def test_capabilities_are_probed_for_every_gpu(every_encoder_works):
    # 上位のGPUができない処理を次のGPUへ回すため、全GPUを調べる
    detection = detect_hardware_encoders(os_name="Windows", cpu_vendor="intel")
    assert [c.api for c in detection.candidates] == ["nvenc", "amf", "qsv"]
    for gpu in detection.candidates:
        assert set(gpu.capabilities) == {"h264", "mpeg2"}
    assert detection.candidates[2].supports("mpeg2", interlaced=False) is True  # mpeg2_qsv
    assert detection.candidates[0].supports("mpeg2", interlaced=False) is False  # NVENCはMPEG-2なし
    assert detection.hardware_id  # 判定時の構成を記録する


def test_hardware_fingerprint_changes_when_a_gpu_is_added(tmp_path):
    def card(name, vendor, device):
        d = tmp_path / name / "device"
        d.mkdir(parents=True)
        (d / "vendor").write_text(vendor)
        (d / "device").write_text(device)

    card("card0", "0x1002", "0x1681")
    (tmp_path / "card0-HDMI-A-1").mkdir()  # 出力端子は数えない
    before = hardware_fingerprint(os_name="Linux", sys_drm=tmp_path)
    assert hardware_fingerprint(os_name="Linux", sys_drm=tmp_path) == before
    card("card1", "0x10de", "0x2882")
    assert hardware_fingerprint(os_name="Linux", sys_drm=tmp_path) != before


@pytest.mark.skipif(platform.system() != "Windows", reason="Windows専用")
def test_windows_fingerprint_lists_display_adapters_quickly():
    import time

    start = time.perf_counter()
    fingerprint = hardware_fingerprint()
    assert time.perf_counter() - start < 0.5  # 起動のたびに呼ぶので軽いこと
    assert "PCI\\VEN_" in fingerprint


def test_no_hardware_means_software_only(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu_module, "list_available_encoders", lambda ffmpeg_path="ffmpeg": [])
    detection = detect_hardware_encoders(os_name="Linux", cpu_vendor="amd", sys_drm=tmp_path)
    assert detection.selected is None
    assert choose_encoder(detection, "h264", interlaced=False) == "libx264"


# ---- エンコーダー名・引数 ---------------------------------------------------------

@pytest.mark.parametrize(
    "encoder,expected",
    [("h264_vaapi", "vaapi"), ("mpeg2_qsv", "qsv"), ("h264_nvenc", "nvenc"), ("h264_amf", "amf"),
     ("libx264", "none"), ("mpeg2video", "none")],
)
def test_encoder_gpu_type(encoder, expected):
    assert encoder_gpu_type(encoder) == expected


@pytest.mark.parametrize(
    "encoder,expected",
    [
        ("libx264", ["-crf", "20"]),
        ("h264_nvenc", ["-rc", "vbr", "-cq", "20", "-b:v", "0"]),
        ("h264_qsv", ["-global_quality", "20"]),
        ("h264_vaapi", ["-rc_mode", "CQP", "-qp", "20"]),
        ("mpeg2video", ["-q:v", "3"]),
        ("mpeg2_qsv", ["-q:v", "3"]),
    ],
)
def test_quality_args_per_encoder(encoder, expected):
    assert quality_args(encoder, 20) == expected


def test_vaapi_needs_device_and_upload_filter():
    assert hw_input_args("h264_vaapi", "/dev/dri/renderD129") == ["-vaapi_device", "/dev/dri/renderD129"]
    assert hw_filter("h264_vaapi") == "format=nv12,hwupload"
    for encoder in ("h264_qsv", "h264_nvenc", "libx264"):
        assert hw_input_args(encoder) == []
        assert hw_filter(encoder) is None


# ---- 実際のffmpegでの動作確認 ------------------------------------------------------

def test_list_available_encoders_includes_libx264():
    assert "libx264" in list_available_encoders()
    assert is_encoder_available("libx264") is True
    assert is_encoder_available("not_a_real_encoder") is False


def test_probe_encoder_functional():
    assert probe_encoder_functional("libx264") is True
    assert probe_encoder_functional("not_a_real_encoder") is False
    assert probe_encoder_functional("libx264", ffmpeg_path="/nonexistent/ffmpeg_xyz") is False


@pytest.mark.parametrize("encoder", ["libx264", "mpeg2video"])
def test_software_encoders_can_encode_interlaced(encoder):
    assert probe_interlaced_encoding(encoder) is True


def test_vaapi_probe_false_without_device(monkeypatch):
    monkeypatch.setattr(gpu_module, "find_vaapi_device", lambda: None)
    assert probe_encoder_functional("h264_vaapi") is False


@pytest.mark.skipif(find_vaapi_device() is None, reason="VAAPIデバイスが無い環境")
def test_real_detection_on_this_machine():
    # 実機での判定(開発PC: AMD APU)。VAAPIのエンコーダーはインターレースの
    # まま符号化できないことを、試しエンコードの結果として確認する。
    detection = detect_hardware_encoders()
    if detection.selected is None or detection.selected.api != "vaapi":
        pytest.skip("VAAPIで動くGPUが無い環境")
    assert detection.selected.supports("h264", interlaced=False) is True
    assert detection.selected.supports("h264", interlaced=True) is False


@pytest.mark.parametrize("encoder", ["h264_qsv", "mpeg2_qsv", "h264_nvenc", "h264_amf"])
def test_real_gpu_encoder_accepts_its_quality_args(encoder):
    # 実機(QSV/NVENC/AMF対応GPUあり)での回帰テスト。probeは品質オプションを
    # 付けずに試すため、mpeg2_qsvが-global_quality(ICQ)を受け付けず
    # 固定品質エンコードが失敗することを見逃していた(Windows実機で確認)。
    if not probe_encoder_functional(encoder):
        pytest.skip(f"{encoder}が使えるGPUが無い環境")
    import subprocess

    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30000/1001:d=0.5",
         "-c:v", encoder, *quality_args(encoder, 23), "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stderr


def test_interlaced_probe_accepts_frame_pictures():
    # libx264はインターレースを1フレーム1パケット(フレームピクチャ)で出せる
    assert probe_interlaced_encoding("libx264") is True


def test_interlaced_probe_rejects_field_pair_output():
    # h264_qsvはインターレースをトップ/ボトムの2パケットで出し、カット処理の
    # つなぎ目合わせが失敗した(Windows実機、2026-10-05) -> インターレース不可とみなす
    if not probe_encoder_functional("h264_qsv"):
        pytest.skip("h264_qsvが使えるGPUが無い環境")
    assert probe_interlaced_encoding("h264_qsv") is False
