from ts_smartcut.system.dependency_checker import DependencyChecker


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
