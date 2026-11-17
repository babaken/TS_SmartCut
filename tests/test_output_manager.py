from datetime import datetime
from pathlib import Path

import pytest

from ts_smartcut.core.output_manager import OutputManager, validate_filename_pattern
from ts_smartcut.errors import FileIOError, ValidationError
from ts_smartcut.models import AppSettings


def test_generate_output_path_default_pattern(tmp_path):
    settings = AppSettings()
    mgr = OutputManager(settings)
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"x")

    output = mgr.generate_output_path(input_path)

    assert output == tmp_path / "video_cut.mp4"


def test_generate_output_path_user_specified(tmp_path):
    settings = AppSettings()
    mgr = OutputManager(settings)
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"x")
    user_out = tmp_path / "custom_name.mp4"

    assert mgr.generate_output_path(input_path, user_specified=user_out) == user_out


def test_generate_output_path_uses_default_output_dir(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    settings = AppSettings(default_output_dir=out_dir)
    mgr = OutputManager(settings)
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"x")

    output = mgr.generate_output_path(input_path)

    assert output == out_dir / "video_cut.mp4"


def test_generate_output_path_renumbers_on_existing_file(tmp_path):
    settings = AppSettings()
    mgr = OutputManager(settings)
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"x")
    (tmp_path / "video_cut.mp4").write_bytes(b"existing")

    output = mgr.generate_output_path(input_path)

    assert output == tmp_path / "video_cut_001.mp4"


def test_generate_output_path_renumbers_repeatedly(tmp_path):
    settings = AppSettings()
    mgr = OutputManager(settings)
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"x")
    (tmp_path / "video_cut.mp4").write_bytes(b"existing")
    (tmp_path / "video_cut_001.mp4").write_bytes(b"existing")

    output = mgr.generate_output_path(input_path)

    assert output == tmp_path / "video_cut_002.mp4"


def test_generate_output_path_never_reuses_input_path_via_pattern(tmp_path):
    # パターンが入出力を区別しない設定でも、入力パスとの衝突は
    # 自動的に連番回避される（design doc §9.4 / CLAUDE.md: 元ファイル保護）。
    settings = AppSettings(output_filename_pattern="{name}{ext}")
    mgr = OutputManager(settings)
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"x")

    output = mgr.generate_output_path(input_path)

    assert output != input_path
    assert output == tmp_path / "video_001.mp4"


def test_generate_output_path_user_specified_same_as_input_raises(tmp_path):
    settings = AppSettings()
    mgr = OutputManager(settings)
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"x")

    with pytest.raises(ValidationError) as exc_info:
        mgr.generate_output_path(input_path, user_specified=input_path)
    assert exc_info.value.code == "E201"


@pytest.mark.parametrize(
    "pattern, expected",
    [
        ("{name}_cut{ext}", "rec_cut.ts"),
        # 拡張子を変える指定をしても元ファイルと同じ(TS→MP4のような変換は目的外)
        ("{name}_cut.mp4", "rec_cut.ts"),
        ("{name}_cut", "rec_cut.ts"),
    ],
)
def test_output_extension_is_always_the_source_extension(tmp_path, pattern, expected):
    source = tmp_path / "rec.ts"
    source.write_bytes(b"")
    manager = OutputManager(AppSettings(output_filename_pattern=pattern))
    assert manager.generate_output_path(source).name == expected


def test_a_dot_in_the_name_is_not_an_extension(tmp_path):
    # {ext} の無いパターンで、名前の「.」の後ろを拡張子として置き換えて名前が切れた(2026-11-17)
    source = tmp_path / "Show 1.5 final.ts"
    source.write_bytes(b"")
    manager = OutputManager(AppSettings(output_filename_pattern="{name}_cut"))
    assert manager.generate_output_path(source).name == "Show 1.5 final_cut.ts"
    manager = OutputManager(AppSettings(output_filename_pattern="{name}"))
    other = tmp_path / "Show 1.5.ts"
    other.write_bytes(b"")
    assert manager.generate_output_path(other, reserved=[other]).name == "Show 1.5_001.ts"


def test_user_specified_output_keeps_source_extension(tmp_path):
    source = tmp_path / "rec.m2ts"
    source.write_bytes(b"")
    manager = OutputManager(AppSettings())
    assert manager.generate_output_path(source, tmp_path / "out.mp4").suffix == ".m2ts"


# ---- ファイル名パターン (design doc §9.2) ----------------------------------------

def test_date_and_time_are_the_processing_time(tmp_path):
    source = tmp_path / "rec.ts"
    source.write_bytes(b"")
    manager = OutputManager(AppSettings(output_filename_pattern="{name}_{date}_{time}{ext}"))
    output = manager.generate_output_path(source, now=datetime(2026, 10, 5, 9, 3, 7))
    assert output.name == "rec_20261005_090307.ts"


def test_date_defaults_to_now(tmp_path):
    source = tmp_path / "rec.ts"
    source.write_bytes(b"")
    manager = OutputManager(AppSettings(output_filename_pattern="{name}_{date}{ext}"))
    assert manager.generate_output_path(source).name == f"rec_{datetime.now():%Y%m%d}.ts"


@pytest.mark.parametrize("pattern", ["{name}_cut{ext}", "{name}{ext}", "{date}_{name}", "{name}_{time}{ext}"])
def test_valid_patterns(pattern):
    assert validate_filename_pattern(pattern) is None


@pytest.mark.parametrize(
    "pattern, message",
    [
        ("", "空"),
        ("{name}_{foo}{ext}", "{foo}"),
        ("{name}_{}", "{}"),
        ("{name_cut{ext}", "対応"),
        ("{name}}", "対応"),
        ("{date}{ext}", "{name}"),
        ("sub/{name}", "/"),
        ("sub\\{name}", "\\"),
        ("{name}:{date}", ":"),
        ("{name!r}", "書式"),
    ],
)
def test_invalid_patterns_are_explained(pattern, message):
    error = validate_filename_pattern(pattern)
    assert error is not None and message in error


def test_invalid_saved_pattern_falls_back_to_default(tmp_path):
    # 手で編集した設定ファイル等に誤りがあってもバッチ処理は止めない
    source = tmp_path / "rec.ts"
    source.write_bytes(b"")
    manager = OutputManager(AppSettings(output_filename_pattern="{name}_{foo}{ext}"))
    assert manager.generate_output_path(source).name == "rec_cut.ts"


def test_check_output_path_explains_what_is_wrong(tmp_path):
    # 保存名称のダイアログ(2026-10-11): 誤りなら注意を出して直してもらう
    mgr = OutputManager(AppSettings())
    source = tmp_path / "rec.ts"
    source.write_bytes(b"x")
    (tmp_path / "exists.ts").write_bytes(b"x")
    other = tmp_path / "other.ts"

    assert mgr.check_output_path(tmp_path / "new.ts", source, [other]) is None
    assert "空" in mgr.check_output_path(tmp_path / ".ts", source)
    assert "?" in mgr.check_output_path(tmp_path / "a?b.ts", source)
    assert "Windows" in mgr.check_output_path(tmp_path / "CON.ts", source)
    assert "元の録画" in mgr.check_output_path(source, source)
    assert "既にあります" in mgr.check_output_path(tmp_path / "exists.ts", source)
    assert "他の項目" in mgr.check_output_path(other, source, [other])


def test_default_name_avoids_reserved_names_and_named_output_is_numbered(tmp_path):
    mgr = OutputManager(AppSettings())
    source = tmp_path / "rec.ts"
    source.write_bytes(b"x")
    first = mgr.generate_output_path(source)
    # バッチの他の項目が使う名前は避ける(同じ録画の2つ目の話の初期値)
    assert mgr.generate_output_path(source, reserved=[first]) == first.with_name(f"{first.stem}_001.ts")
    # 保存名称の名前のファイルが追加の後でできていたら、上書きもエラーもせず連番(2026-10-22)
    first.write_bytes(b"done")
    assert mgr.generate_output_path(source, user_specified=first) == first.with_name(f"{first.stem}_001.ts")
    reserved = [first.with_name(f"{first.stem}_001.ts")]
    assert mgr.generate_output_path(source, user_specified=first, reserved=reserved) == first.with_name(
        f"{first.stem}_002.ts")
    assert first.read_bytes() == b"done"


# ---- place_output: 一時フォルダで作った出力を出力先へ置く (2026-10-22) -------------

def _joined(tmp_path: Path, data: bytes = b"joined") -> Path:
    run_dir = tmp_path / "temp" / "run_x"
    run_dir.mkdir(parents=True, exist_ok=True)
    joined = run_dir / "output.ts"
    joined.write_bytes(data)
    return joined


def test_place_output_moves_the_joined_file(tmp_path):
    source = tmp_path / "rec.ts"
    source.write_bytes(b"x")
    joined = _joined(tmp_path)
    target = tmp_path / "out" / "rec_cut.ts"
    target.parent.mkdir()

    placed = OutputManager(AppSettings()).place_output(joined, target, source)

    assert placed == target and target.read_bytes() == b"joined"
    assert not joined.exists()


def test_place_output_numbers_instead_of_overwriting(tmp_path):
    # 変換中に同じ名前のファイルができていたら、上書きせず連番の名前にする
    source = tmp_path / "rec.ts"
    source.write_bytes(b"x")
    target = tmp_path / "rec_cut.ts"
    target.write_bytes(b"someone else's file")
    target.with_name("rec_cut_001.ts").write_bytes(b"another file")

    placed = OutputManager(AppSettings()).place_output(_joined(tmp_path), target, source,
                                                       reserved=[tmp_path / "rec_cut_002.ts"])

    assert placed == tmp_path / "rec_cut_003.ts" and placed.read_bytes() == b"joined"
    assert target.read_bytes() == b"someone else's file"
    assert target.with_name("rec_cut_001.ts").read_bytes() == b"another file"


def test_place_output_never_uses_the_source_path(tmp_path):
    source = tmp_path / "rec.ts"
    source.write_bytes(b"original")
    source.unlink()  # 元の録画が見えなくなっていても、その名前には置かない
    placed = OutputManager(AppSettings()).place_output(_joined(tmp_path), source, source)
    assert placed == tmp_path / "rec_001.ts"


def test_place_output_copies_when_the_temp_folder_is_on_another_drive(tmp_path, monkeypatch):
    import ts_smartcut.core.output_manager as output_manager

    def cross_device(src, dst):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(output_manager.os, "replace", cross_device)
    source = tmp_path / "rec.ts"
    source.write_bytes(b"x")
    target = tmp_path / "rec_cut.ts"

    placed = OutputManager(AppSettings()).place_output(_joined(tmp_path), target, source)

    assert placed == target and target.read_bytes() == b"joined"


def test_place_output_failure_removes_only_its_own_file(tmp_path, monkeypatch):
    import ts_smartcut.core.output_manager as output_manager

    def fail(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(output_manager.os, "replace", fail)
    monkeypatch.setattr(output_manager.shutil, "copyfile", fail)
    source = tmp_path / "rec.ts"
    source.write_bytes(b"x")
    target = tmp_path / "rec_cut.ts"
    target.write_bytes(b"someone else's file")

    with pytest.raises(FileIOError) as error:
        OutputManager(AppSettings()).place_output(_joined(tmp_path), target, source)

    assert error.value.code == "E002"
    assert target.read_bytes() == b"someone else's file"
    assert not target.with_name("rec_cut_001.ts").exists()
