import pytest

from ts_smartcut.system import dependency_checker as dependency_checker_module
from ts_smartcut.system.dependency_checker import MIN_FFMPEG_MAJOR, DependencyChecker, ffmpeg_major_version


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
    ("7.1.5-12-g1fdbca85aa", "7.1.5-12-g1fdbca85aa"),
    ("n8.1.3", None),
    ("N-121234-g0123456789", None),  # 読めない版では警告しない
])
def test_old_ffmpeg_version(monkeypatch, version, old):
    monkeypatch.setattr(dependency_checker_module.DependencyChecker, "_run_version",
                        staticmethod(lambda exe, args, timeout=5: f"ffmpeg version {version} Copyright (c) 2000-2026\n"))
    assert DependencyChecker.old_ffmpeg_version() == old


def test_old_ffmpeg_version_none_when_missing():
    assert DependencyChecker.old_ffmpeg_version("/nonexistent/ffmpeg_binary_xyz") is None


def test_installed_ffmpeg_is_new_enough():
    # テストの環境(手元・CI)は必要な版を満たす
    assert DependencyChecker.old_ffmpeg_version() is None
    assert MIN_FFMPEG_MAJOR == 8
