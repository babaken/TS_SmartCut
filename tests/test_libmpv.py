"""自作の libmpv の結び付け (ts_smartcut/mpv/libmpv.py、設計書 §2.5)。

本物の libmpv(描画なし: vo=null, ao=null)。mpv はモジュールで 1 つだけ作り、最後のテストで終わらせる
(CLAUDE.md の「1 モジュールに 1 つ」)。描画の API は tests/test_mpv_controller.py が本物の OpenGL で確かめる。"""

import threading
import time

import pytest

from ts_smartcut.mpv import libmpv


def test_node_round_trip():
    builder = libmpv._NodeBuilder()
    value = {"a": [1, 2.5, "三", True, None], "b": {"c": "d"}, "e": []}
    assert libmpv._from_node(builder.node(value)) == value


def test_node_rejects_unknown_types():
    with pytest.raises(TypeError):
        libmpv._NodeBuilder().node(object())


def test_command_args_are_strings():
    assert [libmpv._command_arg(v) for v in ("x", 1, 2.5, True, False)] == ["x", "1", "2.5", "yes", "no"]


def test_library_loads():
    assert libmpv.is_available()
    major, _minor = libmpv.version()
    assert major >= 1


@pytest.fixture(scope="module")
def mpv():
    m = libmpv.Mpv(vo="null", ao="null", idle="yes", pause="yes")
    yield m
    m.terminate()


def test_properties_read_and_write(mpv):
    mpv.volume = 55
    assert mpv.volume == pytest.approx(55)
    mpv.speed = 1.5
    assert mpv.get_property("speed") == pytest.approx(1.5)
    assert mpv.pause is True
    mpv.pause = False
    assert mpv.pause is False
    mpv.pause = True
    assert isinstance(mpv.track_list, list)


def test_unavailable_property_is_none_and_unknown_is_attribute_error(mpv):
    assert mpv.time_pos is None  # ファイルを開いていない
    with pytest.raises(AttributeError):
        _ = mpv.no_such_property_here
    assert not hasattr(mpv, "no_such_property_here")


def test_options_from_the_constructor(mpv):
    assert mpv.get_property("idle") in (True, "yes")  # 版によって flag か文字
    assert mpv.idle_active is True


def test_command_and_errors(mpv):
    assert mpv.command("expand-text", "${speed}") is not None
    with pytest.raises(libmpv.MpvError):
        mpv.command("no-such-command")
    with pytest.raises(libmpv.MpvError):
        mpv.set_property("volume", "not a number")


def test_observe_and_unobserve(mpv):
    seen = []
    changed = threading.Event()

    def on_volume(name, value):
        seen.append((name, value, threading.current_thread().name))
        changed.set()

    mpv.observe_property("volume", on_volume)
    changed.wait(2)  # 監視を始めた時に今の値が一度届く
    changed.clear()
    mpv.volume = 31
    assert changed.wait(2)
    assert seen[-1][0] == "volume" and seen[-1][1] == pytest.approx(31)
    assert seen[-1][2] == "libmpv-events"  # 知らせのスレッドから呼ばれる

    mpv.unobserve_property("volume", on_volume)
    count = len(seen)
    mpv.volume = 32
    time.sleep(0.3)
    assert len(seen) == count


def test_a_failing_observer_does_not_stop_the_events(mpv):
    got = threading.Event()

    def bad(_name, _value):
        raise RuntimeError("boom")

    def good(_name, value):
        if value == pytest.approx(1.25):
            got.set()

    mpv.observe_property("speed", bad)
    mpv.observe_property("speed", good)
    mpv.speed = 1.25
    assert got.wait(2)
    mpv.unobserve_property("speed", bad)
    mpv.unobserve_property("speed", good)


def test_terminate_stops_the_event_thread_first(mpv):
    """最後に実行する(モジュールの mpv を終わらせる)。"""
    thread = mpv._event_thread
    assert thread.is_alive()
    mpv.terminate()
    assert not thread.is_alive()
    assert mpv._handle is None
    mpv.terminate()  # 2 回目は何もしない
    with pytest.raises(libmpv.MpvError):
        _ = mpv.volume
