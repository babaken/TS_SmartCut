import pytest

from ts_smartcut.system import dependency_checker as dependency_checker_module
from ts_smartcut.ffmpeg.compat import MIN_FFMPEG_MAJOR
from ts_smartcut.system.dependency_checker import DependencyChecker, ffmpeg_major_version


def test_check_ffmpeg_true_when_on_path():
    assert DependencyChecker.check_ffmpeg() is True


def test_check_ffprobe_true_when_on_path():
    assert DependencyChecker.check_ffprobe() is True


def test_check_mpv_true_when_on_path():
    assert DependencyChecker.check_mpv() is True


def test_check_bogus_path_returns_false():
    assert DependencyChecker.check_ffmpeg("/nonexistent/ffmpeg_binary_xyz") is False


def test_check_all():
    result = DependencyChecker.check_all()
    assert result == {"ffmpeg": True, "ffprobe": True, "mpv": True}


def test_get_versions_parses_real_output():
    versions = DependencyChecker.get_versions()
    for tool in ("ffmpeg", "ffprobe", "mpv"):
        assert versions[tool] != "Not found"
        assert versions[tool] != "Unknown"
        assert versions[tool][0].isdigit() or versions[tool][0] == "n"


def test_get_versions_not_found_for_bogus_path():
    versions = DependencyChecker.get_versions(mpv_path="/nonexistent/mpv_binary_xyz")
    assert versions["mpv"] == "Not found"


@pytest.mark.parametrize("version, major", [
    ("n8.1.3-35-gf1acb0a266-20261115", 8),        # BtbN
    ("9.0.2-essentials_build-www.gyan.dev", 9),   # gyan.dev (Chocolatey)
    ("6.1.1-3ubuntu5", 6),                        # Ubuntu 24.04
    ("7:6.1.1-3ubuntu5", 6),                      # Debian の epoch 付き
    ("7.1", 7),
    ("N-121234-g0123456789", None),               # 開発版: 版が読めない
    ("2026-11-01-git-0123456789", None),
])
def test_ffmpeg_major_version(version, major):
    assert ffmpeg_major_version(version) == major


@pytest.mark.parametrize("version, old", [
    ("n5.1.6-16-g6e63e49496", "n5.1.6-16-g6e63e49496"),
    ("6.1.1-3ubuntu5", None),  # Ubuntu 24.04 の標準
    ("n8.1.3", None),
    ("N-121234-g0123456789", None),  # 読めない版では警告しない
])
def test_old_ffmpeg_version(monkeypatch, version, old):
    DependencyChecker.clear_ffmpeg_version_cache()
    monkeypatch.setattr(dependency_checker_module.DependencyChecker, "_run_version",
                        staticmethod(lambda exe, args, timeout=5: f"ffmpeg version {version} Copyright (c) 2000-2026\n"))
    assert DependencyChecker.old_ffmpeg_version() == old


def test_old_ffmpeg_version_none_when_missing():
    assert DependencyChecker.old_ffmpeg_version("/nonexistent/ffmpeg_binary_xyz") is None


def test_installed_ffmpeg_is_new_enough():
    # テストの環境(手元・CI)は必要な版を満たす。古い版での確かめ(手元の WSL)では飛ばす
    old = DependencyChecker.old_ffmpeg_version()
    if old is not None:
        pytest.skip(f"ffmpeg {old} での確かめ")
    assert MIN_FFMPEG_MAJOR == 6


def test_version_of_the_installed_ffmpeg():
    import shutil

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg が無い")
    assert DependencyChecker.ffmpeg_version()
    build = DependencyChecker.ffmpeg_build_id()
    assert build.startswith(DependencyChecker.ffmpeg_version() + " (") and build.endswith(")")
    assert DependencyChecker.ffmpeg_version("no-such-ffmpeg-xyz") is None
    assert DependencyChecker.ffmpeg_build_id("no-such-ffmpeg-xyz") is None


def test_ffmpeg_version_is_read_once_and_failures_are_not_kept(monkeypatch):
    # 失敗(時間切れなど)を覚えると、アプリを閉じるまで版が分からないままだった(2026-11-17)
    DependencyChecker.clear_ffmpeg_version_cache()
    calls = []
    answers = [None, "ffmpeg version n7.1.5 Copyright\nconfiguration: --enable-libvpl\n"]

    def fake_run(exe, args, timeout=5):
        calls.append(exe)
        return answers[min(len(calls) - 1, 1)]

    monkeypatch.setattr(dependency_checker_module.DependencyChecker, "_run_version", staticmethod(fake_run))
    assert DependencyChecker.ffmpeg_version("ff") is None
    assert DependencyChecker.ffmpeg_version("ff") == "n7.1.5"
    assert DependencyChecker.old_ffmpeg_version("ff") is None
    assert DependencyChecker.ffmpeg_build_id("ff").startswith("n7.1.5 (")
    assert calls == ["ff", "ff"]  # 読めた後は動かさない


def test_ffmpeg_build_id_runs_a_failing_ffmpeg_only_once(monkeypatch):
    # 読めない ffmpeg は時間切れ(20 秒)まで待つので、1 回の確かめで 2 回動かすと 2 倍待った(2026-11-17 のレビュー)
    DependencyChecker.clear_ffmpeg_version_cache()
    calls = []
    monkeypatch.setattr(dependency_checker_module.DependencyChecker, "_run_version",
                        staticmethod(lambda exe, args, timeout=5: calls.append(exe)))
    assert DependencyChecker.ffmpeg_build_id("ff") is None
    assert calls == ["ff"]


def test_ffmpeg_build_id_tells_builds_of_the_same_version_apart(monkeypatch):
    outputs = {"a": "ffmpeg version 7.1 Copyright\nconfiguration: --enable-libvpl\n",
               "b": "ffmpeg version 7.1 Copyright\nconfiguration: --disable-libvpl\n"}
    DependencyChecker.clear_ffmpeg_version_cache()
    monkeypatch.setattr(dependency_checker_module.DependencyChecker, "_run_version",
                        staticmethod(lambda exe, args, timeout=5: outputs[exe]))
    assert DependencyChecker.ffmpeg_build_id("a") != DependencyChecker.ffmpeg_build_id("b")
