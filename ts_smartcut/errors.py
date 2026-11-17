"""Error code taxonomy (design doc §15): E001-E999.

New failure modes get a code in one of the existing categories below,
not a bespoke exception hierarchy.
"""

from __future__ import annotations

# 短い説明(英語。ログ用)。画面には文字の一覧の errors.Exxx を出す
ERROR_DESCRIPTIONS: dict[str, str] = {
    # E001-E005: File I/O
    "E001": "File read error",
    "E002": "File write error",
    "E003": "File permission error",
    "E004": "Insufficient disk space",
    "E005": "File is locked",
    # E101-E105: ffmpeg / mpv
    "E101": "ffmpeg execution error",
    "E102": "ffprobe execution error",
    "E103": "mpv startup error",
    "E104": "IPC communication error",
    "E105": "mpv not available",
    # E201-E206: Validation
    "E201": "Invalid range",
    "E202": "Overlapping range",
    "E203": "Frame out of range",
    "E204": "Start frame is not before end frame",
    "E205": "Script template error",
    "E206": "Unsupported scan type",
    # E301-E303: GPU
    "E301": "GPU initialization error",
    "E302": "Unsupported codec",
    "E303": "Insufficient GPU memory",
    # E401-E403: Project / i18n
    "E401": "Project file corrupted",
    "E402": "Language file missing",
    "E403": "Version mismatch",
    # E501-E506: System
    "E501": "Insufficient memory",
    "E502": "Thread startup error",
    "E503": "VFR detected",
    "E504": "Processing cancelled",
    "E505": "Cannot write to the application folder",
    "E506": "A required component is missing",
    # Fallback
    "E999": "Unexpected error",
}


class TSSmartCutError(Exception):
    """Base exception for all TS_SmartCut errors. Carries a fixed error code.

    文は文字の一覧の項目(key)と値(params)で渡す(設計書 §13.2): message は今の言語の文
    (画面に出す)、log_message は英語の文(ログ・str(e))。message を直接渡した時(外から来た文)は
    どちらもその文。どちらも無ければエラー番号の短い説明。"""

    def __init__(self, code: str, message: str | None = None, *, key: str | None = None, **params):
        if code not in ERROR_DESCRIPTIONS:
            raise ValueError(f"Unknown error code: {code}")
        self.code = code
        self.key = key
        self.params = params
        if key is not None:
            self.message, self.log_message = _texts(key, params, key)
        elif message:
            self.message = self.log_message = message
        else:
            self.message, self.log_message = _texts(f"errors.{code}", {}, ERROR_DESCRIPTIONS[code])
        super().__init__(f"[{code}] {self.log_message}")


def _texts(key: str, params: dict, fallback: str) -> tuple:
    """(今の言語の文, 英語の文)。言語ファイルが読めない時も例外の中で失敗しないよう fallback を使う。"""
    try:
        from ts_smartcut.i18n.language_manager import english, tr

        return tr(key, **params), english(key, **params)
    except Exception:  # noqa: BLE001 - 例外を作る途中で別の例外を出さない
        return fallback, fallback


# E001-E005: File I/O
class FileIOError(TSSmartCutError):
    pass


class DiskSpaceError(FileIOError):
    """E004"""


class FileLockError(FileIOError):
    """E005"""


# E101-E105: ffmpeg / mpv
class FFmpegError(TSSmartCutError):
    pass


class MuxFailedError(FFmpegError):
    """E101"""


class FFprobeError(FFmpegError):
    """E102"""


class MPVError(TSSmartCutError):
    pass


class MPVStartupError(MPVError):
    """E103"""


class MPVIPCError(MPVError):
    """E104"""


class MPVNotAvailableError(MPVError):
    """E105"""


# E201-E206: Validation
class ValidationError(TSSmartCutError):
    pass


# E301-E303: GPU
class GPUError(TSSmartCutError):
    pass


class UnsupportedCodecError(GPUError):
    """E302 - Smart CutでHead/Tailを再エンコードしようとしたが、ソースの
    映像コーデックに対応するエンコーダーマッピングが存在しない場合に送出する。"""


# E401-E403: Project / i18n
class ProjectError(TSSmartCutError):
    pass


class LanguageFileNotFoundError(ProjectError):
    """E402"""


# E501-E506: System
class SystemResourceError(TSSmartCutError):
    pass


class VFRDetectedError(SystemResourceError):
    """E503 - informational: signals that VFR was detected and Smart Cut
    was auto-disabled. May be raised or just used to carry the code/message
    alongside a warning, depending on the call site."""


class ProcessingCancelledError(SystemResourceError):
    """E504 - 利用者が中止した(エラーではない)。実行中のffmpegは終了させてある。"""


# Fallback
class UnknownError(TSSmartCutError):
    def __init__(self, message: str | None = None):
        super().__init__("E999", message)
