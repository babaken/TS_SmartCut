"""libmpv の自作の結び付け (ctypes。python-mpv の代わり、2026-11-15 利用者の決定、設計書 §2.1)。

アプリが使う所だけを持つ: 作成とオプション、プロパティの読み書き(mpv_node)、コマンド、
プロパティの監視(知らせのスレッド)、終了、描画の API(OpenGL)。

python-mpv との違い(置き換えた理由):
- **ハンドルは 1 つ、終わらせる順番を決めている。** python-mpv は知らせのスレッドが別のハンドル
  (mpv_create_client)で待つ間に、本体のハンドルを mpv_terminate_destroy し、知らせのスレッドが
  後からもう一方を壊していた。Windows で終了時に数 % 落ちていた(CLAUDE.md "mpv crashes on Windows")
  のはこの競合と見ていた。ここでは terminate() が知らせのスレッドを止めて(mpv_wakeup)終わるのを
  待ってから、同じスレッドで mpv_terminate_destroy する。壊している間にハンドルを触るスレッドはない。
- 外部の Python パッケージが要らない(libmpv の DLL / .so は今までどおり利用者が入れる)。

libmpv は LC_NUMERIC が "C" であることを求める(呼び出し側の MPVController が作る前に設定する)。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import sys
import threading
from ctypes import (
    CFUNCTYPE,
    POINTER,
    Structure,
    Union,
    byref,
    c_char_p,
    c_double,
    c_int,
    c_int64,
    c_size_t,
    c_uint64,
    c_ulong,
    c_void_p,
    cast,
)
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# client.h の mpv_format
FORMAT_NONE = 0
FORMAT_STRING = 1
FORMAT_FLAG = 3
FORMAT_INT64 = 4
FORMAT_DOUBLE = 5
FORMAT_NODE = 6
FORMAT_NODE_ARRAY = 7
FORMAT_NODE_MAP = 8
FORMAT_BYTE_ARRAY = 9

# client.h の mpv_event_id
EVENT_NONE = 0
EVENT_SHUTDOWN = 1
EVENT_PROPERTY_CHANGE = 22

# client.h の mpv_error
ERROR_PROPERTY_NOT_FOUND = -8
ERROR_PROPERTY_UNAVAILABLE = -10

# render.h の mpv_render_param_type
RENDER_PARAM_INVALID = 0
RENDER_PARAM_API_TYPE = 1
RENDER_PARAM_OPENGL_INIT_PARAMS = 2
RENDER_PARAM_OPENGL_FBO = 3
RENDER_PARAM_FLIP_Y = 4

# Windows で探す DLL の名前(python-mpv と同じ順。mpv-2.dll / libmpv-2.dll が API 2 = mpv 0.35 以降)
WINDOWS_DLL_NAMES = ["mpv-2.dll", "libmpv-2.dll", "mpv-1.dll"]
LINUX_LIBRARY_NAMES = ["mpv"]  # Linux で find_library に渡す名前(libmpv.so.*)


class MpvError(Exception):
    """libmpv の関数がエラーを返した(code は client.h の mpv_error)。"""

    def __init__(self, function: str, code: int, detail: str = ""):
        self.function = function
        self.code = code
        message = f"{function} failed: {error_string(code)} ({code})"
        super().__init__(f"{message}: {detail}" if detail else message)


# ---- 構造体 ----------------------------------------------------------------------


class MpvNode(Structure):
    pass


class MpvNodeList(Structure):
    _fields_ = [("num", c_int), ("values", POINTER(MpvNode)), ("keys", POINTER(c_char_p))]


class MpvByteArray(Structure):
    _fields_ = [("data", c_void_p), ("size", c_size_t)]


class MpvNodeUnion(Union):
    _fields_ = [
        ("string", c_char_p),
        ("flag", c_int),
        ("int64", c_int64),
        ("double_", c_double),
        ("list", POINTER(MpvNodeList)),
        ("ba", POINTER(MpvByteArray)),
    ]


MpvNode._fields_ = [("u", MpvNodeUnion), ("format", c_int)]


class MpvEvent(Structure):
    _fields_ = [("event_id", c_int), ("error", c_int), ("reply_userdata", c_uint64), ("data", c_void_p)]


class MpvEventProperty(Structure):
    _fields_ = [("name", c_char_p), ("format", c_int), ("data", c_void_p)]


class MpvRenderParam(Structure):
    _fields_ = [("type", c_int), ("data", c_void_p)]


GetProcAddressFn = CFUNCTYPE(c_void_p, c_void_p, c_char_p)
RenderUpdateFn = CFUNCTYPE(None, c_void_p)


class MpvOpenGLInitParams(Structure):
    # extra_exts は API 1 (mpv-1.dll) にだけあった。API 2 は読まないので、どちらにも NULL で渡す
    _fields_ = [("get_proc_address", GetProcAddressFn), ("get_proc_address_ctx", c_void_p), ("extra_exts", c_void_p)]


class MpvOpenGLFBO(Structure):
    _fields_ = [("fbo", c_int), ("w", c_int), ("h", c_int), ("internal_format", c_int)]


# ---- ライブラリの読み込み ----------------------------------------------------------

_lib = None
_lib_lock = threading.Lock()


def _candidate_paths() -> List[str]:
    if os.name == "nt":
        found = []
        for name in WINDOWS_DLL_NAMES:
            path = ctypes.util.find_library(name)  # PATH を探す
            if path:
                found.append(path)
        # 実行ファイル(.exe、ソースから動かす時はリポジトリの直下)の隣も見る
        base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
        found += [str(base / name) for name in WINDOWS_DLL_NAMES if (base / name).is_file()]
        return found
    # 名前を呼び出しに直接書かない: PyInstaller は find_library() に直接書かれた名前のライブラリを同梱する
    # (作った PC の libmpv と、その部品の ffmpeg・x264 などまで入った)。libmpv は利用者の PC のものを使う
    found = []
    for name in LINUX_LIBRARY_NAMES:
        path = ctypes.util.find_library(name)
        if path:
            found.append(path)
    return found


def _declare(lib) -> None:
    def fn(name, argtypes, restype):
        f = getattr(lib, name)
        f.argtypes = argtypes
        f.restype = restype

    fn("mpv_client_api_version", [], c_ulong)
    fn("mpv_error_string", [c_int], c_char_p)
    fn("mpv_create", [], c_void_p)
    fn("mpv_initialize", [c_void_p], c_int)
    fn("mpv_set_option_string", [c_void_p, c_char_p, c_char_p], c_int)
    fn("mpv_terminate_destroy", [c_void_p], None)
    fn("mpv_wait_event", [c_void_p, c_double], POINTER(MpvEvent))
    fn("mpv_wakeup", [c_void_p], None)
    fn("mpv_observe_property", [c_void_p, c_uint64, c_char_p, c_int], c_int)
    fn("mpv_unobserve_property", [c_void_p, c_uint64], c_int)
    fn("mpv_get_property", [c_void_p, c_char_p, c_int, c_void_p], c_int)
    fn("mpv_set_property", [c_void_p, c_char_p, c_int, c_void_p], c_int)
    fn("mpv_command_node", [c_void_p, POINTER(MpvNode), POINTER(MpvNode)], c_int)
    fn("mpv_free_node_contents", [POINTER(MpvNode)], None)
    fn("mpv_render_context_create", [POINTER(c_void_p), c_void_p, POINTER(MpvRenderParam)], c_int)
    fn("mpv_render_context_render", [c_void_p, POINTER(MpvRenderParam)], c_int)
    fn("mpv_render_context_report_swap", [c_void_p], None)
    fn("mpv_render_context_set_update_callback", [c_void_p, RenderUpdateFn, c_void_p], None)
    fn("mpv_render_context_free", [c_void_p], None)


def load_library():
    """libmpv を読み込む(1 回だけ)。見つからない・読めない時は OSError。"""
    global _lib
    with _lib_lock:
        if _lib is not None:
            return _lib
        candidates = _candidate_paths()
        if not candidates:
            names = ", ".join(WINDOWS_DLL_NAMES) if os.name == "nt" else "libmpv"
            raise OSError(f"libmpv not found ({names})")
        errors = []
        for path in candidates:
            try:
                lib = ctypes.CDLL(path)
                _declare(lib)
            except (OSError, AttributeError) as e:
                errors.append(f"{path}: {e}")
                continue
            version = lib.mpv_client_api_version()
            logger.info("Loaded libmpv %s (client API %d.%d)", path, version >> 16, version & 0xFFFF)
            _lib = lib
            return lib
        raise OSError("Could not load libmpv: " + "; ".join(errors))


def is_available() -> bool:
    try:
        load_library()
    except OSError:
        return False
    return True


def error_string(code: int) -> str:
    try:
        text = load_library().mpv_error_string(code)
    except OSError:
        return "unknown error"
    return text.decode("utf-8", "replace") if text else "unknown error"


def _check(function: str, code: int, detail: str = "") -> None:
    if code < 0:
        raise MpvError(function, code, detail)


# ---- mpv_node と Python の値 ---------------------------------------------------------


def _from_node(node: MpvNode) -> Any:
    fmt = node.format
    if fmt == FORMAT_NONE:
        return None
    if fmt == FORMAT_STRING:
        return node.u.string.decode("utf-8", "replace") if node.u.string is not None else ""
    if fmt == FORMAT_FLAG:
        return bool(node.u.flag)
    if fmt == FORMAT_INT64:
        return node.u.int64
    if fmt == FORMAT_DOUBLE:
        return node.u.double_
    if fmt in (FORMAT_NODE_ARRAY, FORMAT_NODE_MAP):
        lst = node.u.list.contents
        values = [_from_node(lst.values[i]) for i in range(lst.num)]
        if fmt == FORMAT_NODE_ARRAY:
            return values
        keys = [lst.keys[i].decode("utf-8", "replace") for i in range(lst.num)]
        return dict(zip(keys, values))
    if fmt == FORMAT_BYTE_ARRAY:
        ba = node.u.ba.contents
        return ctypes.string_at(ba.data, ba.size)
    return None


class _NodeBuilder:
    """Python の値から mpv_node を作る。作った ctypes のものは、使い終わるまでこの中に持つ。"""

    def __init__(self):
        self._keep: List[Any] = []

    def node(self, value: Any) -> MpvNode:
        n = MpvNode()
        self.fill(n, value)
        return n

    def fill(self, n: MpvNode, value: Any) -> None:
        if value is None:
            n.format = FORMAT_NONE
        elif isinstance(value, bool):
            n.format = FORMAT_FLAG
            n.u.flag = int(value)
        elif isinstance(value, int):
            n.format = FORMAT_INT64
            n.u.int64 = value
        elif isinstance(value, float):
            n.format = FORMAT_DOUBLE
            n.u.double_ = value
        elif isinstance(value, (str, os.PathLike)):
            data = os.fspath(value).encode("utf-8")
            self._keep.append(data)
            n.format = FORMAT_STRING
            n.u.string = data
        elif isinstance(value, (list, tuple, dict)):
            items = list(value.items()) if isinstance(value, dict) else [(None, v) for v in value]
            values = (MpvNode * max(len(items), 1))()
            for i, (_key, item) in enumerate(items):
                self.fill(values[i], item)
            lst = MpvNodeList(num=len(items), values=cast(values, POINTER(MpvNode)))
            self._keep += [values, lst]
            if isinstance(value, dict):
                keys = (c_char_p * max(len(items), 1))(*[str(k).encode("utf-8") for k, _ in items])
                self._keep.append(keys)
                lst.keys = cast(keys, POINTER(c_char_p))
                n.format = FORMAT_NODE_MAP
            else:
                n.format = FORMAT_NODE_ARRAY
            n.u.list = ctypes.pointer(lst)
        else:
            raise TypeError(f"cannot pass {type(value).__name__} to mpv")


def _command_arg(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return os.fspath(value) if isinstance(value, os.PathLike) else str(value)


# ---- mpv のハンドル ----------------------------------------------------------------


class Mpv:
    """mpv のハンドル 1 つ。プロパティは属性でも読み書きできる(time_pos → time-pos)。"""

    def __init__(self, **options: Any):
        lib = load_library()
        handle = lib.mpv_create()
        if not handle:
            raise MpvError("mpv_create", -1)
        try:
            for name, value in options.items():
                key = name.replace("_", "-").encode("utf-8")
                _check("mpv_set_option_string", lib.mpv_set_option_string(handle, key, _command_arg(value).encode("utf-8")),
                       name)
            _check("mpv_initialize", lib.mpv_initialize(handle))
        except Exception:
            lib.mpv_terminate_destroy(handle)
            raise
        object.__setattr__(self, "_lib", lib)
        object.__setattr__(self, "_handle", handle)
        object.__setattr__(self, "_observers", {})  # id → (名前, 関数)
        object.__setattr__(self, "_next_id", 1)
        object.__setattr__(self, "_observer_lock", threading.Lock())
        object.__setattr__(self, "_stopping", False)
        thread = threading.Thread(target=self._event_loop, name="libmpv-events", daemon=True)
        object.__setattr__(self, "_event_thread", thread)
        thread.start()

    @property
    def handle(self) -> int:
        if self._handle is None:
            raise MpvError("mpv", -1, "the handle has been terminated")
        return self._handle

    # ---- プロパティ ----

    def get_property(self, name: str) -> Any:
        """プロパティの値。今は使えない(ファイルを開く前の time-pos 等)時は None。"""
        node = MpvNode()
        code = self._lib.mpv_get_property(self.handle, name.encode("utf-8"), FORMAT_NODE, byref(node))
        if code == ERROR_PROPERTY_UNAVAILABLE:
            return None
        if code == ERROR_PROPERTY_NOT_FOUND:
            raise AttributeError(f"mpv has no property {name!r}")
        _check("mpv_get_property", code, name)
        try:
            return _from_node(node)
        finally:
            self._lib.mpv_free_node_contents(byref(node))

    def set_property(self, name: str, value: Any) -> None:
        builder = _NodeBuilder()
        node = builder.node(value)
        code = self._lib.mpv_set_property(self.handle, name.encode("utf-8"), FORMAT_NODE, byref(node))
        if code == ERROR_PROPERTY_NOT_FOUND:
            raise AttributeError(f"mpv has no property {name!r}")
        _check("mpv_set_property", code, name)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return self.get_property(name.replace("_", "-"))

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            self.set_property(name.replace("_", "-"), value)

    # ---- コマンド ----

    def command(self, *args: Any) -> Any:
        builder = _NodeBuilder()
        node = builder.node([_command_arg(a) for a in args])
        result = MpvNode()
        _check("mpv_command_node", self._lib.mpv_command_node(self.handle, byref(node), byref(result)), args[0])
        try:
            return _from_node(result)
        finally:
            self._lib.mpv_free_node_contents(byref(result))

    def seek(self, amount: float, reference: str = "relative", precision: str = "default-precise") -> None:
        self.command("seek", repr(float(amount)), f"{reference}+{precision}")

    # ---- 監視(知らせのスレッド) ----

    def observe_property(self, name: str, callback: Callable[[str, Any], None]) -> None:
        """name が変わるたび、知らせのスレッドから callback(名前, 値) を呼ぶ。"""
        with self._observer_lock:
            observer_id = self._next_id
            object.__setattr__(self, "_next_id", observer_id + 1)
            self._observers[observer_id] = (name, callback)
        _check("mpv_observe_property",
               self._lib.mpv_observe_property(self.handle, observer_id, name.encode("utf-8"), FORMAT_NODE), name)

    def unobserve_property(self, name: str, callback: Callable[[str, Any], None]) -> None:
        with self._observer_lock:
            ids = [i for i, (n, cb) in self._observers.items() if n == name and cb == callback]
            for observer_id in ids:
                del self._observers[observer_id]
        for observer_id in ids:
            self._lib.mpv_unobserve_property(self.handle, observer_id)

    def _event_loop(self) -> None:
        lib, handle = self._lib, self._handle
        while not self._stopping:
            event = lib.mpv_wait_event(handle, -1).contents
            if event.event_id == EVENT_SHUTDOWN:
                return
            if event.event_id != EVENT_PROPERTY_CHANGE or self._stopping:
                continue
            with self._observer_lock:
                observer = self._observers.get(event.reply_userdata)
            if observer is None:
                continue
            prop = cast(event.data, POINTER(MpvEventProperty)).contents
            value = _from_node(cast(prop.data, POINTER(MpvNode)).contents) if prop.format == FORMAT_NODE and prop.data else None
            try:
                observer[1](observer[0], value)
            except Exception:  # noqa: BLE001 - 知らせの関数の失敗でスレッドを止めない
                logger.exception("mpv property observer failed: %s", observer[0])

    # ---- 終了 ----

    def terminate(self) -> None:
        """知らせのスレッドを止めて終わるのを待ってから、ハンドルを壊す(この順番。モジュールの説明)。
        描画のコンテキスト(MpvRenderContext)は先に free() しておくこと。"""
        handle = self._handle
        if handle is None:
            return
        if threading.current_thread() is self._event_thread:
            raise RuntimeError("terminate() must not be called from the mpv event thread")
        object.__setattr__(self, "_stopping", True)
        self._lib.mpv_wakeup(handle)
        self._event_thread.join()
        object.__setattr__(self, "_handle", None)
        self._lib.mpv_terminate_destroy(handle)


class MpvRenderContext:
    """描画の API(OpenGL)。呼び出し側の GL コンテキストがカレントの時に作り・描き・free() する。"""

    def __init__(self, mpv: Mpv, get_proc_address: "GetProcAddressFn"):
        self._lib = mpv._lib
        self._get_proc_address = get_proc_address  # C から呼ばれる間は持っておく
        self._update_fn: Optional[RenderUpdateFn] = None
        init = MpvOpenGLInitParams(get_proc_address, None, None)
        api = c_char_p(b"opengl")
        params = (MpvRenderParam * 3)(
            MpvRenderParam(RENDER_PARAM_API_TYPE, cast(api, c_void_p)),
            MpvRenderParam(RENDER_PARAM_OPENGL_INIT_PARAMS, cast(ctypes.pointer(init), c_void_p)),
            MpvRenderParam(RENDER_PARAM_INVALID, None),
        )
        ctx = c_void_p()
        _check("mpv_render_context_create", self._lib.mpv_render_context_create(byref(ctx), mpv.handle, params))
        self._ctx = ctx

    def render(self, fbo: int, width: int, height: int, flip_y: bool = True, internal_format: int = 0) -> None:
        target = MpvOpenGLFBO(fbo, width, height, internal_format)
        flip = c_int(1 if flip_y else 0)
        params = (MpvRenderParam * 3)(
            MpvRenderParam(RENDER_PARAM_OPENGL_FBO, cast(ctypes.pointer(target), c_void_p)),
            MpvRenderParam(RENDER_PARAM_FLIP_Y, cast(ctypes.pointer(flip), c_void_p)),
            MpvRenderParam(RENDER_PARAM_INVALID, None),
        )
        _check("mpv_render_context_render", self._lib.mpv_render_context_render(self._ctx, params))

    def report_swap(self) -> None:
        self._lib.mpv_render_context_report_swap(self._ctx)

    def set_update_callback(self, callback: Optional[Callable[[], None]]) -> None:
        """新しいフレームがある時に mpv のスレッドから呼ばれる(Qt へは信号で渡すこと)。"""
        func = callback or (lambda: None)
        self._update_fn = RenderUpdateFn(lambda _ctx: func())
        self._lib.mpv_render_context_set_update_callback(self._ctx, self._update_fn, None)

    def free(self) -> None:
        if self._ctx is not None:
            self._lib.mpv_render_context_free(self._ctx)
            self._ctx = None


__all__ = ["Mpv", "MpvRenderContext", "MpvError", "GetProcAddressFn", "load_library", "is_available"]


def version() -> Tuple[int, int]:
    """libmpv のクライアント API の版(読み込めない時は OSError)。"""
    v = load_library().mpv_client_api_version()
    return v >> 16, v & 0xFFFF

