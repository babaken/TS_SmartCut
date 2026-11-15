"""MPEG-2のフィールド表示フラグ書き換え(ffmpeg/mpeg2_flags.py)。"""

import subprocess

import pytest

from ts_smartcut.ffmpeg.mpeg2_flags import (
    PictureFlags,
    count_pictures,
    frame_rate_code,
    read_picture_flags,
    rewrite_field_flags,
)


@pytest.fixture(scope="module")
def progressive_es(tmp_path_factory):
    path = tmp_path_factory.mktemp("m2v") / "p.m2v"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=d=0.5:s=160x120:r=24000/1001",
         "-c:v", "mpeg2video", "-bf", "0", "-f", "mpeg2video", str(path)],
        check=True, capture_output=True,
    )
    return path.read_bytes()


@pytest.fixture(scope="module")
def interlaced_es(tmp_path_factory):
    path = tmp_path_factory.mktemp("m2v") / "i.m2v"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=d=0.5:s=160x120:r=30000/1001",
         "-c:v", "mpeg2video", "-bf", "0", "-flags", "+ilme+ildct", "-f", "mpeg2video", str(path)],
        check=True, capture_output=True,
    )
    return path.read_bytes()


def test_rewrite_sets_soft_telecine_flags(progressive_es):
    n = count_pictures(progressive_es)
    cadence = [PictureFlags(True, True, True), PictureFlags(True, False, False),
               PictureFlags(True, False, True), PictureFlags(True, True, False)]
    flags = [cadence[k % 4] for k in range(n)]

    out = rewrite_field_flags(progressive_es, flags, rate_code=4)

    assert read_picture_flags(out) == flags
    assert len(out) == len(progressive_es)  # ビットの書き換えのみで長さは変わらない


def test_rewrite_rejects_picture_count_mismatch(progressive_es):
    n = count_pictures(progressive_es)
    with pytest.raises(ValueError):
        rewrite_field_flags(progressive_es, [PictureFlags(True, True, False)] * (n - 1))


def test_rewrite_rejects_repeat_on_interlaced_picture(interlaced_es):
    # 規格上、繰り返し指示はプログレッシブのコマにしか付けられない
    n = count_pictures(interlaced_es)
    with pytest.raises(ValueError):
        rewrite_field_flags(interlaced_es, [PictureFlags(False, True, True)] * n)


def test_rewrite_rejects_progressive_on_field_dct_picture(interlaced_es):
    # インターレース(フィールドDCT)で符号化したコマをプログレッシブとは名乗れない
    n = count_pictures(interlaced_es)
    with pytest.raises(ValueError):
        rewrite_field_flags(interlaced_es, [PictureFlags(True, True, False)] * n)


@pytest.mark.parametrize("fps, code", [(30000 / 1001, 4), (24000 / 1001, 1), (25.0, 3), (60000 / 1001, 7)])
def test_frame_rate_code(fps, code):
    assert frame_rate_code(fps) == code
