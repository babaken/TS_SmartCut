import pytest

from ts_smartcut.models import CutRange
from ts_smartcut.ffmpeg.smart_cut import (
    MIN_TAIL_FRAMES,
    MODE_SIMPLE_COPY,
    MODE_SMART_CUT,
    SegmentPart,
    compute_keep_segments,
    contiguous_runs,
    coarse_seek_frame,
    plan_full_reencode_parts,
    plan_simple_copy_parts,
    plan_smart_cut_parts,
    resolve_processing_mode,
)


# ---- compute_keep_segments -------------------------------------------------

def test_compute_keep_segments_no_cuts():
    assert compute_keep_segments(100, []) == [(0, 100)]


def test_compute_keep_segments_single_middle_cut():
    assert compute_keep_segments(100, [CutRange(30, 45)]) == [(0, 30), (45, 100)]


def test_compute_keep_segments_cut_at_start():
    assert compute_keep_segments(100, [CutRange(0, 20)]) == [(20, 100)]


def test_compute_keep_segments_cut_at_end():
    assert compute_keep_segments(100, [CutRange(80, 100)]) == [(0, 80)]


def test_compute_keep_segments_entire_file_cut():
    assert compute_keep_segments(100, [CutRange(0, 100)]) == []


def test_compute_keep_segments_multiple_cuts():
    cuts = [CutRange(10, 20), CutRange(50, 60), CutRange(90, 95)]
    assert compute_keep_segments(100, cuts) == [(0, 10), (20, 50), (60, 90), (95, 100)]


def test_compute_keep_segments_unsorted_input_is_sorted():
    cuts = [CutRange(50, 60), CutRange(10, 20)]
    assert compute_keep_segments(100, cuts) == [(0, 10), (20, 50), (60, 100)]


def test_compute_keep_segments_overlapping_cuts_are_merged():
    cuts = [CutRange(10, 30), CutRange(20, 40)]
    assert compute_keep_segments(100, cuts) == [(0, 10), (40, 100)]


def test_compute_keep_segments_out_of_range_is_clamped():
    cuts = [CutRange(-10, 20)]
    assert compute_keep_segments(100, cuts) == [(20, 100)]


def test_compute_keep_segments_zero_total_frames():
    assert compute_keep_segments(0, [CutRange(0, 0)]) == []


# ---- plan_smart_cut_parts --------------------------------------------------

def test_plan_smart_cut_parts_segment_starts_and_ends_on_keyframes():
    # [0, 30) with keyframes at every 10 frames -> pure Body copy, no Head/Tail.
    keyframes = [0, 10, 20, 30, 40, 50, 60, 70]
    parts = plan_smart_cut_parts([(0, 30)], keyframes)
    assert parts == [SegmentPart("copy", "Body", 0, 30)]


def test_plan_smart_cut_parts_head_body_tail():
    keyframes = [0, 10, 20, 30, 40, 50, 60, 70]
    parts = plan_smart_cut_parts([(45, 79)], keyframes)
    assert parts == [
        SegmentPart("reencode", "Head", 45, 50),
        SegmentPart("copy", "Body", 50, 70),
        SegmentPart("reencode", "Tail", 70, 79),
    ]


def test_plan_smart_cut_parts_no_keyframe_in_segment_is_single_reencode():
    # Segment entirely inside one GOP -> no interior keyframe boundary.
    keyframes = [0, 10, 20, 30]
    parts = plan_smart_cut_parts([(12, 18)], keyframes)
    assert parts == [SegmentPart("reencode", "Reencode", 12, 18)]


def test_plan_smart_cut_parts_multiple_segments_concatenated_in_order():
    keyframes = [0, 10, 20, 30, 40, 50, 60, 70]
    parts = plan_smart_cut_parts([(0, 30), (45, 79)], keyframes)
    assert parts == [
        SegmentPart("copy", "Body", 0, 30),
        SegmentPart("reencode", "Head", 45, 50),
        SegmentPart("copy", "Body", 50, 70),
        SegmentPart("reencode", "Tail", 70, 79),
    ]


def test_plan_smart_cut_parts_empty_segment_list():
    assert plan_smart_cut_parts([], [0, 10, 20]) == []


def test_plan_smart_cut_parts_no_keyframes_at_all():
    parts = plan_smart_cut_parts([(0, 50)], [])
    assert parts == [SegmentPart("reencode", "Reencode", 0, 50)]


# ---- plan_simple_copy_parts -------------------------------------------------

def test_plan_simple_copy_parts_snaps_to_next_keyframe():
    keyframes = [0, 10, 20, 30, 40, 50, 60, 70]
    parts = plan_simple_copy_parts([(0, 30), (45, 75)], keyframes)
    assert parts == [
        SegmentPart("copy", "Copy", 0, 30),
        SegmentPart("copy", "Copy", 50, 75),
    ]


def test_plan_simple_copy_parts_no_keyframe_after_start_yields_nothing():
    keyframes = [0, 10, 20]
    parts = plan_simple_copy_parts([(25, 30)], keyframes)
    assert parts == []


# ---- plan_full_reencode_parts -----------------------------------------------

def test_plan_full_reencode_parts():
    parts = plan_full_reencode_parts([(0, 30), (45, 75)])
    assert parts == [
        SegmentPart("reencode", "Reencode", 0, 30),
        SegmentPart("reencode", "Reencode", 45, 75),
    ]


def test_plan_full_reencode_parts_skips_empty_segments():
    assert plan_full_reencode_parts([(5, 5)]) == []


# ---- resolve_processing_mode -------------------------------------------------

def test_resolve_processing_mode_smart_cut_non_vfr_unchanged():
    mode, warning = resolve_processing_mode(MODE_SMART_CUT, is_vfr=False)
    assert mode == MODE_SMART_CUT
    assert warning is None


def test_resolve_processing_mode_smart_cut_vfr_forces_simple_copy():
    mode, warning = resolve_processing_mode(MODE_SMART_CUT, is_vfr=True)
    assert mode == MODE_SIMPLE_COPY
    assert warning is not None
    assert "VFR" in warning or "可変フレームレート" in warning


def test_resolve_processing_mode_simple_copy_vfr_unaffected():
    mode, warning = resolve_processing_mode(MODE_SIMPLE_COPY, is_vfr=True)
    assert mode == MODE_SIMPLE_COPY
    assert warning is None


# ---- 末尾・オープンGOP・Simple Copyの終端 (2026-10-04) -------------------------

def test_plan_smart_cut_copies_through_end_of_file_instead_of_tail():
    # 末尾まで続く区間は最後のキーフレーム以降もcopyし、Tailを作らない
    # (以前は105..120を無駄に再エンコードしていた)。
    keyframes = [0, 15, 30, 45, 60, 75, 90, 105]
    parts = plan_smart_cut_parts([(52, 120)], keyframes, total_frames=120)
    assert parts == [
        SegmentPart("reencode", "Head", 52, 60),
        SegmentPart("copy", "Body", 60, 120),
    ]


def test_plan_smart_cut_still_has_tail_when_segment_ends_before_eof():
    keyframes = [0, 15, 30, 45, 60, 75, 90, 105]
    parts = plan_smart_cut_parts([(52, 115)], keyframes, total_frames=120)
    assert parts[-1] == SegmentPart("reencode", "Tail", 105, 115)


def test_plan_smart_cut_moves_open_gop_leading_pictures_into_tail():
    # キーフレーム60の直前2フレームはオープンGOPのリーディングピクチャで、
    # 60の後ろに格納されるためBodyのcopyに含められない -> Tailで再エンコード。
    keyframes = [0, 15, 30, 45, 60, 75]
    parts = plan_smart_cut_parts([(20, 70)], keyframes, total_frames=90, leading_pictures={60: 2})
    assert parts == [
        SegmentPart("reencode", "Head", 20, 30),
        SegmentPart("copy", "Body", 30, 58),
        SegmentPart("reencode", "Tail", 58, 70),
    ]


def test_plan_simple_copy_snaps_end_back_to_keyframe_when_requested():
    keyframes = [0, 10, 20, 30, 40, 50, 60, 70]
    parts = plan_simple_copy_parts(
        [(0, 35), (45, 75)], keyframes, total_frames=100, snap_end=True, leading_pictures={30: 2}
    )
    assert parts == [
        SegmentPart("copy", "Copy", 0, 28),
        SegmentPart("copy", "Copy", 50, 70),
    ]


def test_plan_simple_copy_does_not_snap_segment_reaching_eof():
    keyframes = [0, 10, 20, 30]
    parts = plan_simple_copy_parts([(5, 37)], keyframes, total_frames=37, snap_end=True)
    assert parts == [SegmentPart("copy", "Copy", 10, 37)]


def test_coarse_seek_frame_lands_well_before_target():
    keyframes = [0, 15, 30, 45, 60, 75, 90, 105]
    # 目標108の2秒(60フレーム)手前=48以下のキーフレームのうち最後から2番目
    assert coarse_seek_frame(108, keyframes, 30.0) == 30
    # 目標が先頭付近ならシークしない
    assert coarse_seek_frame(40, keyframes, 30.0) == 0


def test_contiguous_runs_merges_head_body_tail():
    parts = [
        SegmentPart("copy", "Body", 0, 13),
        SegmentPart("reencode", "Tail", 13, 20),
        SegmentPart("reencode", "Head", 40, 45),
        SegmentPart("copy", "Body", 45, 58),
    ]
    assert contiguous_runs(parts) == [(0, 20), (40, 58)]


def test_plan_smart_cut_short_tail_moves_body_end_to_earlier_keyframe():
    # キーフレーム60ちょうどで終わる区間: リーディングピクチャ2枚だけのTailは
    # copyに挟まれてデコード時刻の余地が無い(1フィールド未満に詰まった) ->
    # 1つ前のキーフレーム45でBodyを区切り、Tailを1GOP分長くする。
    keyframes = [0, 15, 30, 45, 60, 75]
    parts = plan_smart_cut_parts([(20, 60)], keyframes, total_frames=90, leading_pictures={45: 2, 60: 2})
    assert parts == [
        SegmentPart("reencode", "Head", 20, 30),
        SegmentPart("copy", "Body", 30, 43),
        SegmentPart("reencode", "Tail", 43, 60),
    ]
    assert MIN_TAIL_FRAMES > 2


def test_plan_smart_cut_short_tail_without_earlier_keyframe_reencodes_segment():
    keyframes = [0, 15, 30, 45]
    parts = plan_smart_cut_parts([(20, 32)], keyframes, total_frames=90, leading_pictures={30: 2})
    assert parts == [SegmentPart("reencode", "Reencode", 20, 32)]


def test_progress_maps_output_frames_to_source_frames():
    # エンコード中にプレビュー・シークバーを処理中の位置へ動かすため、出力済みの
    # フレーム数を元のフレーム番号へ変える(spansを順につないだ位置)
    from ts_smartcut.ffmpeg.smart_cut import SmartCutEngine

    events = []
    on_frames = SmartCutEngine._progress("Concat", 4, 5, events.append, [(10, 20), (50, 70)])
    assert (events[0].phase_progress, events[0].source_frame) == (0.0, 10)  # 始まりを知らせる
    on_frames(5)
    on_frames(15)
    on_frames(30)
    on_frames(40)  # 予定より多くても最後で止まる
    assert [e.source_frame for e in events[1:]] == [15, 55, 69, 69]
    assert [round(e.phase_progress, 3) for e in events[1:]] == [0.167, 0.5, 1.0, 1.0]
    assert all(e.part_index == 4 and e.total_parts == 5 for e in events)

    audio = []
    assert SmartCutEngine._progress("Audio", 3, 5, audio.append) is not None
    assert audio[0].source_frame is None  # 映像を伴わない段階
