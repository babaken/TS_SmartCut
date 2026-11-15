"""ショートカットキーのプリセット (design doc §4.5.1)。

キーの割り当てを 5 つまでのプリセットに持ち、名前を付けて切り替える。どのプリセットも最初は
今までのキー(DEFAULT_KEYS)で、リセットするとそれに戻る。1 つの操作に割り当てるキーは 1 つまで
(キーボードモードの左手デバイス・ゲームパッドへ一部の操作を移す使い方。2026-11-15 利用者の決定)。
同じキーを別の操作に入れると、前の操作からは外す(重なったキーは Qt がどちらも動かさないため)。

保存先は settings.json ではなく同じフォルダの shortcut_presets.json(AppSettings はプロジェクト
ファイルにも埋め込まれ、キーの割り当てまでプロジェクトごとに保存されてしまうため)。キーは
QKeySequence の PortableText("Ctrl+Z" など)で書く。読み書きに失敗してもアプリは止めない
(警告ログだけで、既定のキーで動く)。"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ts_smartcut.core.settings_manager import get_config_dir

logger = logging.getLogger(__name__)

PRESET_COUNT = 5
FILE_VERSION = 1

# (操作, 既定のキー)。並びは F1 の一覧と設定のタブの並び。空のキーは割り当てなし
# (案の承認・却下は 2026-11-15 に増やした操作で、今までキーが無かったため既定もなし)
ACTIONS: List[tuple] = [
    ("play_pause", "Space"),
    ("frame_back", "Left"),
    ("frame_forward", "Right"),
    ("prev_keyframe", "<"),
    ("next_keyframe", ">"),
    ("speed_down", "["),
    ("speed_up", "]"),
    ("set_start", "S"),
    ("set_end", "E"),
    ("jump_start", "Home"),
    ("jump_end", "End"),
    ("clear_selection", "Esc"),
    ("add_cut", "I"),
    ("add_bookmark", "B"),
    ("delete_cut", "Del"),
    ("accept_proposal", ""),
    ("reject_proposal", ""),
    ("mute", "M"),
    ("undo", "Ctrl+Z"),
    ("redo", "Ctrl+Y"),
    ("save_project", "Ctrl+S"),
    ("open", "Ctrl+O"),
    ("help", "F1"),
    ("exit", "Ctrl+Q"),
]

ACTION_IDS: List[str] = [a for a, _key in ACTIONS]
DEFAULT_KEYS: Dict[str, str] = dict(ACTIONS)


def get_default_shortcut_presets_path() -> Path:
    return get_config_dir() / "shortcut_presets.json"


def normalize_key(key: str) -> str:
    """キーを PortableText にそろえる(読めないキーは空 = 割り当てなし)。"""
    from PySide6.QtGui import QKeySequence

    if not isinstance(key, str) or not key.strip():
        return ""
    sequence = QKeySequence(key.strip(), QKeySequence.SequenceFormat.PortableText)
    if sequence.isEmpty() or sequence.count() != 1:
        return ""
    return sequence.toString(QKeySequence.SequenceFormat.PortableText)


def display_key(key: str) -> str:
    """画面に出すキーの名前("Left" → "←" など。今までの一覧の書き方に合わせる)。"""
    return {"Left": "←", "Right": "→", "Up": "↑", "Down": "↓"}.get(key, key)


@dataclass
class ShortcutPreset:
    name: str = ""  # 空なら「プリセット n」と表示する
    keys: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_KEYS))

    def key_for(self, action: str) -> str:
        return self.keys.get(action, "")

    def set_key(self, action: str, key: str) -> Optional[str]:
        """action に key を割り当てる。同じキーを持っていた別の操作からは外し、その操作を返す。"""
        if action not in DEFAULT_KEYS:
            raise KeyError(action)
        key = normalize_key(key)
        moved_from = None
        if key:
            for other, other_key in self.keys.items():
                if other != action and other_key == key:
                    self.keys[other] = ""
                    moved_from = other
        self.keys[action] = key
        return moved_from

    def reset(self) -> None:
        """キーを既定(今までのキー)に戻す。名前はそのまま。"""
        self.keys = dict(DEFAULT_KEYS)


class ShortcutPresetManager:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or get_default_shortcut_presets_path()
        self.presets: List[ShortcutPreset] = [ShortcutPreset() for _ in range(PRESET_COUNT)]
        self.active_index = 0
        self._load()

    @property
    def active(self) -> ShortcutPreset:
        return self.presets[self.active_index]

    def key_for(self, action: str) -> str:
        return self.active.key_for(action)

    def snapshot(self) -> "ShortcutPresetManager":
        """設定のタブで編集する写し(OK まで本物を変えない)。"""
        return copy.deepcopy(self)

    def replace_with(self, other: "ShortcutPresetManager") -> None:
        self.presets = copy.deepcopy(other.presets)
        self.active_index = other.active_index
        self.save()

    def set_active(self, index: int) -> None:
        if not 0 <= index < PRESET_COUNT:
            raise IndexError(index)
        self.active_index = index
        self.save()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            presets = data["presets"]
            active = data.get("active", 0)
            if not isinstance(presets, list):
                raise TypeError("presets is not a list")
        except (json.JSONDecodeError, TypeError, KeyError, AttributeError, OSError) as e:
            logger.warning("Could not read the shortcut presets, using the default keys: %s: %s", self.path, e)
            return
        for index, entry in enumerate(presets[:PRESET_COUNT]):
            if isinstance(entry, dict):
                self.presets[index] = self._preset_from(entry)
        if isinstance(active, int) and 0 <= active < PRESET_COUNT:
            self.active_index = active

    @staticmethod
    def _preset_from(entry: dict) -> ShortcutPreset:
        name = entry.get("name", "")
        preset = ShortcutPreset(name=name if isinstance(name, str) else "")
        keys = entry.get("keys", {})
        if not isinstance(keys, dict):
            return preset
        # 書かれていない操作(後から増えた操作)は既定のキー。ただし既定のキーをほかの操作が
        # 使っていれば割り当てなし。知らない操作は捨てる
        written = {a: normalize_key(k) for a, k in keys.items() if a in DEFAULT_KEYS}
        used = set()
        for action in ACTION_IDS:
            if action in written:
                key = written[action]
                if key in used:
                    key = ""
            else:
                key = DEFAULT_KEYS[action]
                if key in used or key in written.values():
                    key = ""
            preset.keys[action] = key
            if key:
                used.add(key)
        return preset

    def save(self) -> None:
        data = {
            "version": FILE_VERSION,
            "active": self.active_index,
            "presets": [{"name": p.name, "keys": {a: p.key_for(a) for a in ACTION_IDS}} for p in self.presets],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            logger.warning("Could not save the shortcut presets: %s: %s", self.path, e)
