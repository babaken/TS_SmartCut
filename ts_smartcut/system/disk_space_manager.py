"""Disk space checks (design doc §17)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import List, Optional

from ts_smartcut.errors import DiskSpaceError
from ts_smartcut.models import CutRange, FileCutInfo

# 見積もりの余裕。コンテナのオーバーヘッドや再エンコード部分のサイズの
# ぶれを吸収する。
SAFETY_MARGIN = 1.1


class DiskSpaceManager:
    @staticmethod
    def check_available_space(path: Path) -> int:
        """pathが属するファイルシステムの空き容量（バイト）を返す。"""
        stat = shutil.disk_usage(path)
        return stat.free

    @staticmethod
    def estimate_required_space(file_info: FileCutInfo, cuts: List[CutRange]) -> int:
        """出力1本分に必要な容量の概算(残す区間の割合×元のサイズ×余裕)を返す。
        処理中は同じ大きさの一時ファイル(映像・音声の部品)も別に必要になる
        ため、置き場所ごとの必要量はcheck_before_processingで合算する。"""
        total = max(file_info.total_frames, 1)
        removed, cursor = 0, 0
        for cut in sorted(cuts, key=lambda c: c.start_frame):
            start = max(cursor, min(max(cut.start_frame, 0), total))
            end = min(max(cut.end_frame, 0), total)
            if end > start:
                removed += end - start
                cursor = end
        kept_ratio = (total - removed) / total
        return int(file_info.file_size * kept_ratio * SAFETY_MARGIN)

    @staticmethod
    def check_before_processing(
        file_info: FileCutInfo,
        cuts: List[CutRange],
        output_dir: Path,
        temp_dir: Optional[Path] = None,
    ) -> bool:
        """容量不足の場合 DiskSpaceError (E004) を送出し、十分な場合 True を返す。
        一時ファイル(部品)と出力はどちらもほぼ出力1本分の容量を使う。出力は一時フォルダで
        結合してから出力先へ移す(2026-10-22)。同じファイルシステムなら移すのは名前の変更
        だけなので合計2本分。別なら一時フォルダに部品と結合した出力の2本分、出力先にコピーの1本分。"""
        need = DiskSpaceManager.estimate_required_space(file_info, cuts)
        if temp_dir is None or DiskSpaceManager._same_filesystem(output_dir, temp_dir):
            DiskSpaceManager._require(output_dir, need * 2)
        else:
            DiskSpaceManager._require(output_dir, need)
            DiskSpaceManager._require(temp_dir, need * 2)
        return True

    @staticmethod
    def _same_filesystem(a: Path, b: Path) -> bool:
        try:
            return os.stat(a).st_dev == os.stat(b).st_dev
        except OSError:
            return False

    @staticmethod
    def _require(path: Path, required: int) -> None:
        available = DiskSpaceManager.check_available_space(path)
        if available < required:
            raise DiskSpaceError("E004", key="exceptions.disk_space", path=path, required=f"{required / 1e9:.1f}",
                                 available=f"{available / 1e9:.1f}")
