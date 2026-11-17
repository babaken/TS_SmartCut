"""ハードウェアエンコーダーの判定と選択 (design doc §6.5)。

方針(利用者の要件、2026-10-05):
- 優先順位は「ビデオカード(dGPU) → CPU内蔵GPU(Intel QSV / AMD APU) →
  ソフトウェアエンコーダー」。判定は初回起動時に1回行い、結果を保存する
  (保存・読み込みはcore/hardware_encoders.py)。GPU・CPUの構成が変わった
  時だけ判定し直す(hardware_fingerprint)。
- 見つかったGPUすべてで、コーデック(MPEG-2 / H.264)ごとに「プログレッシブ」
  「インターレース」それぞれ実際に試しエンコードして、できることを記録する。
  再エンコード時は、必要なこと(コーデック・走査方式)ができるGPUを優先順位
  どおりに探して使い、どのGPUもできなければソフトウェアエンコーダーで処理する
  (例: NVENCはインターレース非対応 → CPU内蔵のQSVでインターレースのまま符号化)。

参考(2026-10-05 実測/調査): ffmpegのVAAPI(AMD/Intel, Linux)のエンコーダーは
インターレース(フィールド)符号化ができず、AMDのハードウェアはMPEG-2を
符号化できない。NVENCはMPEG-2非対応。インターレース対応は試しエンコードで
判定するので、ここでは決め打ちしない。
"""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

from ts_smartcut.i18n.language_manager import english, tr
from ts_smartcut.system.child_processes import no_window
from ts_smartcut.system.temp_file_manager import app_temp_dir

logger = logging.getLogger(__name__)

CPU_ENCODER = "libx264"

# 対応コーデック(日本のテレビ放送のTSはMPEG-2かH.264)
CODECS = ("h264", "mpeg2")
_CPU_ENCODER_MAP = {"h264": "libx264", "mpeg2": "mpeg2video"}

# GPUの種類(API) -> コーデックごとのffmpegエンコーダー名。存在しない組み合わせ
# (NVENC/AMFのMPEG-2など)は含めない。
_GPU_ENCODER_MAP: Dict[str, Dict[str, str]] = {
    "nvenc": {"h264": "h264_nvenc"},
    "amf": {"h264": "h264_amf"},
    "qsv": {"h264": "h264_qsv", "mpeg2": "mpeg2_qsv"},
    "vaapi": {"h264": "h264_vaapi", "mpeg2": "mpeg2_vaapi"},
}

# VAAPIエンコーダーはGPUメモリ上のフレームしか受け付けないため、デバイス指定
# (-vaapi_device)とアップロード用フィルタ(hwupload)が必須。QSV/NVENC/AMFは
# システムメモリ上のフレームをエンコーダー側で受け付けるので不要。
_VAAPI_UPLOAD_FILTER = "format=nv12,hwupload"

# 動作確認(probe)用の解像度。AMDのVCN等は小さすぎる解像度(64x64等)を
# 拒否するため、実際の動画に近い現実的なサイズで試す。
_PROBE_SIZE = "320x240"

# mpeg2videoは-crfに対応しないため-q:v(qscale)を使う。手動検証でクリーンに
# デコードできることを確認したデフォルト値。
MPEG2_DEFAULT_QSCALE = 3

_PCI_VENDORS = {"0x10de": "nvidia", "0x1002": "amd", "0x8086": "intel"}

# 判定方法の版。保存済みの判定結果と違えば判定し直す。
# 1: 最優先のGPUだけ調べていた / 2: 全GPUを調べ、インターレースは1フレーム
# 1パケットで出せることまで確認する(2026-10-05) / 3: インターレースの試しを実際の再エンコードと
# 同じフィルター・指定にし、判定した ffmpeg のビルドを控える(2026-11-17)
DETECTION_VERSION = 3


@dataclass
class HardwareEncoder:
    """GPU1つ分の判定結果。"""

    api: str  # "nvenc" / "amf" / "qsv" / "vaapi"
    vendor: str  # "nvidia" / "amd" / "intel"
    discrete: bool  # ビデオカード(True) / CPU内蔵(False)
    device: Optional[str] = None  # VAAPIのレンダーノード
    # {"h264": {"progressive": True, "interlaced": False}, "mpeg2": {...}}
    capabilities: Dict[str, Dict[str, bool]] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.label_in(tr)

    def label_in(self, text: Callable[..., str]) -> str:
        """text は文字の一覧を引く関数(画面は tr、ログは english)。"""
        kind = text("hardware.discrete") if self.discrete else text("hardware.integrated")
        return text("hardware.label", vendor=self.vendor.upper(), api=self.api.upper(), kind=kind)

    def supports(self, codec: str, interlaced: bool) -> bool:
        return bool(self.capabilities.get(codec, {}).get("interlaced" if interlaced else "progressive"))


@dataclass
class HardwareDetection:
    """初回起動時の判定結果。selectedがNoneならソフトウェアエンコーダーのみ。"""

    selected: Optional[HardwareEncoder] = None
    candidates: List[HardwareEncoder] = field(default_factory=list)
    # 判定した時のGPU・CPUの構成(hardware_fingerprint)。変わったら判定し直す。
    hardware_id: str = ""
    # 判定方法の版(DETECTION_VERSION)。判定方法を変えたら保存済みの結果を使わない。
    version: int = DETECTION_VERSION
    # 判定に使った ffmpeg のビルド(DependencyChecker.ffmpeg_build_id)。ffmpeg を入れ替えたら判定し直す
    # (GPU の部品の有無や動きはビルドで違う、2026-11-17 利用者の決定)。読めなかった時は None
    ffmpeg_build: Optional[str] = None

    @property
    def ordered(self) -> List[HardwareEncoder]:
        """優先順に並んだGPU。"""
        if self.candidates:
            return self.candidates
        return [self.selected] if self.selected else []

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "hardware_id": self.hardware_id,
            "ffmpeg_build": self.ffmpeg_build,
            "selected": asdict(self.selected) if self.selected else None,
            "candidates": [asdict(c) for c in self.candidates],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HardwareDetection":
        def _encoder(d: Optional[dict]) -> Optional[HardwareEncoder]:
            return HardwareEncoder(**d) if d else None

        return cls(
            selected=_encoder(data.get("selected")),
            candidates=[HardwareEncoder(**c) for c in data.get("candidates", [])],
            hardware_id=data.get("hardware_id", ""),
            version=int(data.get("version", 1)),
            ffmpeg_build=data.get("ffmpeg_build"),
        )

    def describe(self, text: Optional[Callable[..., str]] = None) -> str:
        """設定画面・システム情報用の説明。"""
        gpus = self.ordered
        text = text or tr
        if not gpus:
            return text("hardware.none")
        lines = [text("hardware.header")]
        names = {"h264": "H.264", "mpeg2": "MPEG-2"}
        for n, gpu in enumerate(gpus, 1):
            lines.append(f"  {n}. {gpu.label_in(text)}")
            for codec in CODECS:
                p = "○" if gpu.supports(codec, False) else "×"
                i = "○" if gpu.supports(codec, True) else "×"
                lines.append("     " + text("hardware.codec_line", codec=names[codec], progressive=p, interlaced=i))
        lines.append("  " + text("hardware.fallback"))
        return "\n".join(lines)


# ---- エンコーダー名・引数 ---------------------------------------------------

def encoder_gpu_type(encoder: str) -> str:
    """エンコーダー名からGPUの種類("qsv"/"nvenc"/"vaapi"/"amf")を返す。CPUなら"none"。"""
    for api in _GPU_ENCODER_MAP:
        if encoder.endswith(f"_{api}"):
            return api
    return "none"


def hw_input_args(encoder: str, device: Optional[str] = None) -> List[str]:
    """入力(-i)より前に置く必要があるハードウェア初期化オプション。"""
    if encoder_gpu_type(encoder) == "vaapi":
        device = device or find_vaapi_device()
        return ["-vaapi_device", device] if device else []
    return []


def hw_filter(encoder: str) -> Optional[str]:
    """エンコーダーへ渡す前に必要な映像フィルタ(VAAPIのGPUアップロード)。"""
    return _VAAPI_UPLOAD_FILTER if encoder_gpu_type(encoder) == "vaapi" else None


def quality_args(encoder: str, quality: int) -> List[str]:
    """元ソースのビットレートが分からない場合の固定品質指定(CRF相当の値)を
    エンコーダーごとのオプションに変換する。"""
    api = encoder_gpu_type(encoder)
    if encoder in ("mpeg2video", "mpeg2_qsv"):
        # mpeg2_qsvは-global_quality(ICQ)非対応で失敗する(Windows実機で確認)。
        # -q:vならCQPになり、mpeg2videoと同程度のqscaleとして扱える。
        return ["-q:v", str(MPEG2_DEFAULT_QSCALE)]
    if api == "nvenc":
        return ["-rc", "vbr", "-cq", str(quality), "-b:v", "0"]
    if api == "qsv":
        return ["-global_quality", str(quality)]
    if api == "vaapi":
        return ["-rc_mode", "CQP", "-qp", str(quality)]
    if api == "amf":
        return ["-rc", "cqp", "-qp_i", str(quality), "-qp_p", str(quality)]
    return ["-crf", str(quality)]


def choose_hardware(
    detection: Optional[HardwareDetection], codec: str, interlaced: bool
) -> Optional[HardwareEncoder]:
    """必要なこと(コーデック・走査方式)ができる最優先のGPU。無ければNone。"""
    for gpu in detection.ordered if detection else []:
        if gpu.supports(codec, interlaced) and _GPU_ENCODER_MAP.get(gpu.api, {}).get(codec):
            return gpu
    return None


def choose_encoder(detection: Optional[HardwareDetection], codec: str, interlaced: bool) -> str:
    """使うエンコーダー名。必要なことができるGPUを優先順に探し、どれもできなければ
    ソフトウェアエンコーダー。デバイス(VAAPI)はchoose_hardware()のdeviceを使う
    (同じAPIのGPUが2つある場合、エンコーダー名だけではどちらか決まらない)。"""
    gpu = choose_hardware(detection, codec, interlaced)
    if gpu is not None:
        return _GPU_ENCODER_MAP[gpu.api][codec]
    return _CPU_ENCODER_MAP.get(codec, CPU_ENCODER)


# ---- 判定 ------------------------------------------------------------------

def list_available_encoders(ffmpeg_path: Union[str, Path] = "ffmpeg") -> List[str]:
    """このffmpegビルドに組み込まれているエンコーダー名一覧を返す。
    実ハードウェアの有無ではなくビルド時の対応可否を示す。"""
    try:
        result = subprocess.run(
            [str(ffmpeg_path), "-hide_banner", "-encoders"],
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=5,
            **no_window(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []

    encoders = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0][:1] in ("V", "A", "S"):
            encoders.append(parts[1])
    return encoders


def is_encoder_available(encoder_name: str, ffmpeg_path: Union[str, Path] = "ffmpeg") -> bool:
    return encoder_name in list_available_encoders(ffmpeg_path)


def find_vaapi_device() -> Optional[str]:
    """VAAPIで使うDRMレンダーノード(/dev/dri/renderD*)を返す。無ければNone。"""
    nodes = sorted(Path("/dev/dri").glob("renderD*")) if Path("/dev/dri").is_dir() else []
    return str(nodes[0]) if nodes else None


def probe_encoder_functional(
    encoder_name: str,
    ffmpeg_path: Union[str, Path] = "ffmpeg",
    timeout: float = 10,
    device: Optional[str] = None,
) -> bool:
    """指定エンコーダーで実際に1フレームだけエンコードを試み、真に動作するか
    確認する。ビルドにエンコーダーが組み込まれていても、対応デバイス
    (VAAPIレンダーノードやCUDAライブラリ等)が無ければ実行時に失敗するため、
    ビルド一覧の確認だけでは不十分。"""
    hw_args = hw_input_args(encoder_name, device)
    if encoder_gpu_type(encoder_name) == "vaapi" and not hw_args:
        return False  # VAAPIデバイスが存在しない
    vf = hw_filter(encoder_name)
    try:
        result = subprocess.run(
            [
                str(ffmpeg_path), "-hide_banner", "-loglevel", "error",
                *hw_args,
                "-f", "lavfi", "-i", f"color=c=black:s={_PROBE_SIZE}:d=0.1",
                *(["-vf", vf] if vf else []),
                "-vframes", "1", "-c:v", encoder_name, "-f", "null", "-",
            ],
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
            **no_window(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def probe_interlaced_encoding(
    encoder_name: str,
    ffmpeg_path: Union[str, Path] = "ffmpeg",
    ffprobe_path: Union[str, Path] = "ffprobe",
    device: Optional[str] = None,
    timeout: float = 20,
    source: Optional[Path] = None,
) -> bool:
    """インターレース(トップフィールドが先)のままエンコードできるか。実際の再エンコード
    (ffmpeg/smart_cut.py)と同じフィルターと指定で数フレームを符号化し、出力の全フレームが
    インターレースとして記録されているかで判定する(指定を無視してプログレッシブで出す
    エンコーダーがある)。Smart Cut の Head/Tail(-bf 0)と Full Re-encode(B フレームあり)の両方を試す。

    試しが実際の処理と違うと、判定は ○ なのに変換だけが失敗した: ffmpeg 7.x はフィールドの
    フィルターの setpts の後でフレームレートを失い、h264_qsv が開けなかった(2026-11-16)。これは
    TS から読んだ映像の時だけ起き、テスト画像(lavfi)を直接符号化すると起きない(7.1.5、2026-11-17
    実測)ので、試しもまず小さなインターレースの TS を作り、それを読んで符号化する。
    GPU が使えるかは版の番号ではなく、この試しで決める(2026-11-17 利用者の決定)。
    source は試しの入力(_interlaced_probe_source)。判定では GPU・コーデックごとに同じものを使う(無ければ作る)。"""
    hw_args = hw_input_args(encoder_name, device)
    if encoder_gpu_type(encoder_name) == "vaapi" and not hw_args:
        return False
    variants = [["-bf", "0"], []] if encoder_name.startswith("h264") else [["-bf", "0"]]
    # アプリの一時フォルダ(実行ファイルの横の Temp/)に書く。OS の一時フォルダは使わない(2026-11-16)
    with tempfile.TemporaryDirectory(prefix="probe_", dir=app_temp_dir()) as tmp:
        if source is None:
            source = _interlaced_probe_source(ffmpeg_path, Path(tmp), timeout)
        if source is None:
            return False
        return all(_probe_interlaced_once(encoder_name, ffmpeg_path, ffprobe_path, hw_args, extra, timeout,
                                          source, Path(tmp) / f"probe{n}.ts")
                   for n, extra in enumerate(variants))


# 判定の試しのビットレート(放送の録画と同じくらい)。実際の再エンコードは元のビットレートを渡す
_PROBE_BITRATE = 8_000_000


def _interlaced_probe_source(ffmpeg_path: Union[str, Path], tmp: Path, timeout: float) -> Optional[Path]:
    """試しの入力: インターレース(トップが先)の MPEG-2 の短い TS。mpeg2video はどのビルドにもある。"""
    source = tmp / "source.ts"
    try:
        result = subprocess.run(
            [str(ffmpeg_path), "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", f"testsrc=s={_PROBE_SIZE}:r=60000/1001:d=0.4",
             "-vf", "tinterlace=interleave_top,setfield=tff", "-c:v", "mpeg2video", "-flags", "+ilme+ildct",
             "-b:v", "4000000", str(source)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, **no_window(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not source.exists():
        logger.info("Could not make the interlaced test source: %s", (result.stderr or "").strip()[-300:])
        return None
    return source


def interlaced_probe_filters(encoder_name: str) -> List[str]:
    """判定の試しで、読んだ TS の映像に通すフィルター。実際の再エンコードと同じ。H.264:
    SmartCutEngine._field_filter(repeatfields・setfield・setpts でフレームの刻みに付け直す)。
    MPEG-2: SmartCutEngine._encode_mpeg2_run(setpts で付け直して fps)。"""
    rate = "30000/1001"
    filters: List[str] = []
    if encoder_name.startswith("mpeg2"):
        filters += [f"setpts=N/({rate})/TB", f"fps={rate}"]
    else:
        filters += ["repeatfields", "setfield=tff", f"setpts=N/({rate})/TB"]
    if hw_filter(encoder_name):
        filters.append(hw_filter(encoder_name))
    return filters


def _probe_interlaced_once(encoder_name: str, ffmpeg_path: Union[str, Path], ffprobe_path: Union[str, Path],
                           hw_args: List[str], extra: List[str], timeout: float, source: Path, out: Path) -> bool:
    rate_args = ["-b:v", str(_PROBE_BITRATE), "-maxrate", str(int(_PROBE_BITRATE * 1.5)),
                 "-bufsize", str(_PROBE_BITRATE * 2)]
    passthrough = [] if encoder_name.startswith("mpeg2") else ["-fps_mode", "passthrough"]
    try:
        encode = subprocess.run(
            [
                str(ffmpeg_path), "-hide_banner", "-loglevel", "error", *hw_args,
                "-copyts", "-i", str(source), "-map", "0:v:0",
                "-vf", ",".join(interlaced_probe_filters(encoder_name)), *passthrough,
                "-c:v", encoder_name, "-flags", "+ilme+ildct", *extra, *rate_args, str(out),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            **no_window(),
        )
        if encode.returncode != 0 or not out.exists():
            logger.info("Interlaced test encode failed: %s %s: %s", encoder_name, " ".join(extra),
                        (encode.stderr or "").strip()[-300:])
            return False
        probe = subprocess.run(
            [str(ffprobe_path), "-v", "quiet", "-select_streams", "v:0", "-count_packets",
             "-show_entries", "frame=interlaced_frame:stream=nb_read_packets", "-of", "json", str(out)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            **no_window(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    try:
        data = json.loads(probe.stdout or "{}")
        frames = data.get("frames", [])
        packets = int((data.get("streams") or [{}])[0].get("nb_read_packets", -1))
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    # 1フレームが1パケット(フレームピクチャ)であること。h264_qsvはトップ/ボトムを
    # 別々のピクチャ(2パケット)で出し、パケット単位でつなぎ目を合わせるカット
    # 処理(_fit_decode_times等)が失敗した(Windows実機、2026-10-05)。
    ok = bool(frames) and packets == len(frames) and all(f.get("interlaced_frame") == 1 for f in frames)
    if not ok:
        logger.info("Interlaced test encode not usable: %s %s: %d frames, %d packets, %d interlaced",
                    encoder_name, " ".join(extra), len(frames), packets,
                    sum(1 for f in frames if f.get("interlaced_frame") == 1))
    return ok


def _cpu_vendor() -> str:
    """CPUのメーカー("amd"/"intel"/"")。"""
    try:
        text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    if "AuthenticAMD" in text:
        return "amd"
    if "GenuineIntel" in text:
        return "intel"
    # Windows: "AMD64 Family 25 Model 80 Stepping 0, AuthenticAMD" 等
    processor = platform.processor()
    if "AuthenticAMD" in processor or processor.startswith("AMD"):
        return "amd"
    if "GenuineIntel" in processor or "Intel" in processor:
        return "intel"
    return ""


def _linux_vaapi_devices(sys_drm: Path = Path("/sys/class/drm")) -> List[dict]:
    """VAAPIで使えそうなレンダーノードと、メーカー・PCI位置・映像用メモリ量。"""
    devices = []
    for node in sorted(sys_drm.glob("renderD*")):
        try:
            vendor_id = (node / "device" / "vendor").read_text().strip()
        except OSError:
            continue
        pci = os.path.basename(os.path.realpath(node / "device"))
        try:
            vram = int((node / "device" / "mem_info_vram_total").read_text().strip())
        except (OSError, ValueError):
            vram = 0
        devices.append({
            "device": f"/dev/dri/{node.name}",
            "vendor": _PCI_VENDORS.get(vendor_id, "other"),
            "pci": pci,
            "vram": vram,
        })
    return devices


def _find_candidates(
    ffmpeg_path: Union[str, Path], os_name: str, cpu_vendor: str, sys_drm: Path
) -> List[HardwareEncoder]:
    """この環境で実際に動くGPUを集め、ビデオカードかCPU内蔵かを推定する。

    ビデオカード/CPU内蔵の見分け(近似):
    - NVIDIA: 常にビデオカード
    - Intel: PCI位置00:02.0ならCPU内蔵、それ以外(Arc)はビデオカード
    - AMD: CPUがIntelならビデオカード。CPUがAMDなら、AMDのGPUが1つならAPU
      (CPU内蔵)とみなし、2つ以上なら映像用メモリが最大のものをビデオカードとする
      (APUのメモリ量はBIOS設定次第で大きくなるため、単独では見分けられない)
    """
    available = set(list_available_encoders(ffmpeg_path))
    candidates: List[HardwareEncoder] = []

    if "h264_nvenc" in available and probe_encoder_functional("h264_nvenc", ffmpeg_path):
        candidates.append(HardwareEncoder("nvenc", "nvidia", True))

    if os_name == "Windows":
        if "h264_amf" in available and probe_encoder_functional("h264_amf", ffmpeg_path):
            candidates.append(HardwareEncoder("amf", "amd", cpu_vendor != "amd"))
        if "h264_qsv" in available and probe_encoder_functional("h264_qsv", ffmpeg_path):
            candidates.append(HardwareEncoder("qsv", "intel", False))
        return candidates

    devices = _linux_vaapi_devices(sys_drm)
    amd = [d for d in devices if d["vendor"] == "amd"]
    largest_amd = max(amd, key=lambda d: d["vram"]) if len(amd) > 1 else None
    for d in devices:
        if d["vendor"] == "amd":
            discrete = cpu_vendor != "amd" or d is largest_amd
        elif d["vendor"] == "intel":
            discrete = not d["pci"].endswith(":00:02.0")
        else:
            continue  # NVIDIAはNVENCで扱う
        if d["vendor"] == "intel" and "h264_qsv" in available and probe_encoder_functional("h264_qsv", ffmpeg_path):
            # Intelは(Linuxでも)QSVが動けばQSVを使う
            candidates.append(HardwareEncoder("qsv", "intel", discrete, None))
            continue
        if "h264_vaapi" in available and probe_encoder_functional("h264_vaapi", ffmpeg_path, device=d["device"]):
            candidates.append(HardwareEncoder("vaapi", d["vendor"], discrete, d["device"]))
    return candidates


def _rank(encoder: HardwareEncoder) -> tuple:
    """並び順: ビデオカード → CPU内蔵。同じ区分の中ではNVIDIA → AMD → Intel。"""
    vendor_order = {"nvidia": 0, "amd": 1, "intel": 2}
    return (0 if encoder.discrete else 1, vendor_order.get(encoder.vendor, 9))


def detect_hardware_encoders(
    ffmpeg_path: Union[str, Path] = "ffmpeg",
    ffprobe_path: Union[str, Path] = "ffprobe",
    os_name: Optional[str] = None,
    cpu_vendor: Optional[str] = None,
    sys_drm: Path = Path("/sys/class/drm"),
) -> HardwareDetection:
    """使えるGPUを優先順位どおりに並べ、それぞれのできることを調べる。"""
    candidates = _find_candidates(
        ffmpeg_path, os_name or platform.system(), cpu_vendor if cpu_vendor is not None else _cpu_vendor(), sys_drm
    )
    candidates.sort(key=_rank)
    fingerprint = hardware_fingerprint(sys_drm=sys_drm)
    from ts_smartcut.system.dependency_checker import DependencyChecker

    build = DependencyChecker.ffmpeg_build_id(ffmpeg_path)
    if not candidates:
        return HardwareDetection(None, [], fingerprint, ffmpeg_build=build)
    # インターレースの試しの入力は 1 回だけ作り、GPU・コーデックごとに使い回す(要る時だけ作る)
    with tempfile.TemporaryDirectory(prefix="probe_", dir=app_temp_dir()) as tmp:
        shared: List[Optional[Path]] = []

        def probe_source() -> Optional[Path]:
            if not shared:
                shared.append(_interlaced_probe_source(ffmpeg_path, Path(tmp), 20))
            return shared[0]

        for gpu in candidates:
            for codec in CODECS:
                encoder = _GPU_ENCODER_MAP.get(gpu.api, {}).get(codec)
                progressive = bool(encoder) and probe_encoder_functional(encoder, ffmpeg_path, device=gpu.device)
                interlaced = False
                if progressive:
                    source = probe_source()
                    interlaced = source is not None and probe_interlaced_encoding(
                        encoder, ffmpeg_path, ffprobe_path, device=gpu.device, source=source
                    )
                gpu.capabilities[codec] = {"progressive": progressive, "interlaced": interlaced}
    return HardwareDetection(candidates[0], candidates, fingerprint, ffmpeg_build=build)


def hardware_fingerprint(os_name: Optional[str] = None, sys_drm: Path = Path("/sys/class/drm")) -> str:
    """GPU・CPUの構成を表す文字列。起動のたびに比べ、変わった時だけ判定し直す
    (試しエンコードはしない。OSの情報を読むだけ — Windowsで数十ms)。"""
    os_name = os_name or platform.system()
    parts: List[str] = []
    if os_name == "Windows":
        parts += _windows_display_adapters()
        parts.append(_windows_cpu_name())
    else:
        for card in sorted(sys_drm.glob("card[0-9]*")):
            if "-" in card.name:
                continue  # card0-HDMI-A-1 等の出力端子
            try:
                vendor = (card / "device" / "vendor").read_text().strip()
                device = (card / "device" / "device").read_text().strip()
            except OSError:
                continue
            parts.append(f"{vendor}:{device}")
        parts.append(_linux_cpu_name())
    return "|".join(sorted(set(p for p in parts if p)))


def _windows_display_adapters() -> List[str]:
    """PCIにつながっている表示アダプター(GPU)の名前とPCIのベンダー/デバイスID。
    モニターがつながっていないCPU内蔵GPUも含む。

    EnumDisplayDevicesはログオン中のセッションのアダプターしか返さず、リモート
    デスクトップでは「Microsoft Remote Display Adapter」だけになる(2026-10-05、
    Windows実機)。ローカルとリモートで構成が変わったと判定して毎回試しエンコード
    し直さないよう、セッションに依らないSetupAPIで、PCIの表示デバイスを数える。"""
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class _Guid(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        class _DevInfoData(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("ClassGuid", _Guid),
                        ("DevInst", wintypes.DWORD), ("Reserved", ctypes.c_size_t)]

        display_class = _Guid()  # GUID_DEVCLASS_DISPLAY
        ctypes.memmove(ctypes.byref(display_class),
                       uuid.UUID("4d36e968-e325-11ce-bfc1-08002be10318").bytes_le, 16)
        setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
        setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
        setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
        setupapi.SetupDiEnumDeviceInfo.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
            ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
        ]
        setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]

        digcf_present = 0x2
        devices = setupapi.SetupDiGetClassDevsW(ctypes.byref(display_class), "PCI", None, digcf_present)
        if not devices or devices == ctypes.c_void_p(-1).value:
            return []

        def registry_property(data, prop: int) -> str:
            buffer = ctypes.create_unicode_buffer(1024)
            if not setupapi.SetupDiGetDeviceRegistryPropertyW(
                devices, ctypes.byref(data), prop, None, buffer, ctypes.sizeof(buffer), None
            ):
                return ""
            return buffer.value  # REG_MULTI_SZは最初の文字列

        adapters = []
        try:
            index = 0
            while True:
                data = _DevInfoData()
                data.cbSize = ctypes.sizeof(data)
                if not setupapi.SetupDiEnumDeviceInfo(devices, index, ctypes.byref(data)):
                    break
                description = registry_property(data, 0)  # SPDRP_DEVICEDESC
                hardware_id = registry_property(data, 1)  # SPDRP_HARDWAREID
                adapters.append(f"{description}/{hardware_id.split('&SUBSYS')[0]}")
                index += 1
        finally:
            setupapi.SetupDiDestroyDeviceInfoList(devices)
        return adapters
    except (OSError, AttributeError, ValueError):
        return []


def _windows_cpu_name() -> str:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
    except (OSError, ImportError):
        return platform.processor()


def _linux_cpu_name() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor()
