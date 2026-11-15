"""設定の「自動カット」タブの「しきい値」 (設計書 §31.9「ルールのしきい値」「しきい値の自動補正を自動で採用する」)。

局(と全局共通)を選んで、判定に使うしきい値の一覧を見る(表示だけ)。値はバッチが終わった後に
自動で補正・採用される(autocut/auto_tuning.py)。手入力・戻す・補正のボタンは 2026-10-15 に無くした
(利用者の決定: 補正は自動でよい、一覧は欲しい)。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFormLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ts_smartcut.autocut.thresholds import (
    DEFAULT,
    GLOBAL,
    MANUAL,
    SPECS,
    STATION,
    ThresholdRow,
    resolve_detail,
    station_key,
)
from ts_smartcut.autocut.tuning import MAX_RECALL_DROP, MIN_TUNE_RECORDINGS
from ts_smartcut.i18n.language_manager import tr

logger = logging.getLogger(__name__)

COL_GROUP, COL_NAME, COL_VALUE, COL_SOURCE, COL_DEFAULT, COL_RANGE = range(6)


def _format(value: float, decimals: int) -> str:
    return f"{value:.{decimals}f}"


def _local_time(iso: Optional[str]) -> Optional[str]:
    """DB の日時(UTC)を、この PC の日付と時刻(分まで)にする。"""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return None


class ThresholdPanel(QWidget):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self._db = db
        self._rows: Dict[str, Dict[str, ThresholdRow]] = {}
        self._times: Dict[tuple, str] = {}
        self._stations = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if db is None:
            layout.addWidget(QLabel(tr("thresholds.no_db")))
            self.station_combo = None
            return
        try:
            self._rows = {k: dict(v) for k, v in db.get_threshold_rows().items()}
            self._times = db.get_threshold_times()
            self._stations = db.list_stations()
        except Exception:  # noqa: BLE001 - 設定ダイアログは開けるようにする
            logger.warning("Could not read the thresholds from the history DB", exc_info=True)

        form = QFormLayout()
        self.station_combo = QComboBox()
        total = sum(s["recordings"] for s in self._stations)
        self.station_combo.addItem(tr("thresholds.all_stations", count=total), ("", total))
        for s in self._stations:
            key = station_key(s["original_network_id"], s["service_id"])
            name = s["service_name"] or tr("thresholds.unknown_station", sid=s["service_id"])
            self.station_combo.addItem(tr("thresholds.station_item", name=name, count=s["recordings"]),
                                       (key, s["recordings"]))
        self.station_combo.currentIndexChanged.connect(self._refresh)
        form.addRow(tr("thresholds.station"), self.station_combo)
        layout.addLayout(form)

        self.table = _FullTable(len(SPECS), 6)
        self.table.setHorizontalHeaderLabels([
            tr("thresholds.col_group"), tr("thresholds.col_name"), tr("thresholds.col_value"),
            tr("thresholds.col_source"), tr("thresholds.col_default"), tr("thresholds.col_range"),
        ])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for i, spec in enumerate(SPECS):
            self.table.setItem(i, COL_GROUP, QTableWidgetItem(tr(f"thresholds.group_{spec.group}")))
            name_item = QTableWidgetItem(tr(f"thresholds.name_{spec.name}"))
            name_item.setToolTip(tr(f"thresholds.tip_{spec.name}"))
            self.table.setItem(i, COL_NAME, name_item)
            self.table.setItem(i, COL_VALUE, QTableWidgetItem())
            self.table.setItem(i, COL_SOURCE, QTableWidgetItem())
            self.table.setItem(i, COL_DEFAULT, QTableWidgetItem(_format(spec.default, spec.decimals)))
            self.table.setItem(i, COL_RANGE, QTableWidgetItem(
                tr("thresholds.range", minimum=_format(spec.minimum, spec.decimals),
                   maximum=_format(spec.maximum, spec.decimals))))
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self.hint_label = QLabel()
        self.hint_label.setWordWrap(True)
        self._refresh()
        _fit_table(self.table)  # 値と「使っている値」を入れてから大きさを測る
        layout.addWidget(self.table)
        layout.addWidget(self.hint_label)

    # ---- 表示 -------------------------------------------------------------

    def current_key(self) -> str:
        return self.station_combo.currentData()[0] if self.station_combo is not None else ""

    def _current_count(self) -> int:
        return self.station_combo.currentData()[1]

    def _source_text(self, key: str, name: str, level: str, source: Optional[str]) -> str:
        if level == DEFAULT:
            return tr("thresholds.source_default")
        where = tr("thresholds.source_station") if level == STATION else tr("thresholds.source_global")
        if source == MANUAL:  # 手入力があった版で書いた値
            return tr("thresholds.source", where=where, kind=tr("thresholds.kind_manual"))
        when = _local_time(self._times.get((key if level == STATION else "", name)))
        kind = tr("thresholds.kind_auto") if when is None else tr("thresholds.kind_auto_at", time=when)
        return tr("thresholds.source", where=where, kind=kind)

    def _refresh(self) -> None:
        key = self.current_key()
        detail = resolve_detail(self._rows, key, warn=False)
        for i, spec in enumerate(SPECS):
            value, level, source = detail[spec.name]
            self.table.item(i, COL_VALUE).setText(_format(value, spec.decimals))
            item = self.table.item(i, COL_SOURCE)
            item.setText(self._source_text(key, spec.name, level, source))
            font = item.font()
            font.setBold(level == STATION or (not key and level == GLOBAL))  # この段に行がある
            item.setFont(font)
        hints = [tr("thresholds.hint_global") if not key else tr("thresholds.hint_station")]
        if key and self._current_count() < MIN_TUNE_RECORDINGS:
            hints.append(tr("thresholds.hint_few", count=MIN_TUNE_RECORDINGS))
        hints.append(tr("thresholds.hint_auto", points=round(MAX_RECALL_DROP * 100)))
        self.hint_label.setText("\n".join(hints))

    def value_text(self, name: str) -> str:
        """一覧に出している値。"""
        return self.table.item([s.name for s in SPECS].index(name), COL_VALUE).text()

    def source_text(self, name: str) -> str:
        """一覧に出している「使っている値」。"""
        return self.table.item([s.name for s in SPECS].index(name), COL_SOURCE).text()

    def set_expanded(self, expanded: bool) -> None:
        """表示中なら一覧が全部見える大きさを希望する(SettingsDialog がタブの切り替えで呼ぶ)。"""
        if self.station_combo is not None:
            self.table.expanded = expanded
            self.table.updateGeometry()


class _FullTable(QTableWidget):
    """expanded の間だけ、行と列がすべて見える大きさを希望の大きさ(sizeHint)にする表。最小の
    大きさにはせず、隠れている時は小さく見せる(QTabWidget は隠れているタブの大きさも数えるので、
    設定ダイアログのほかのタブまで大きくなった)。"""

    full_size: Optional[QSize] = None
    expanded = True

    def sizeHint(self) -> QSize:
        if self.full_size is not None and self.expanded:
            return self.full_size
        return QSize(100, 100)


def _fit_table(table: QTableWidget, fixed_height: bool = False) -> None:
    """表の行と列がすべて見える大きさにする(既定の大きさでは 15 項目のうち 6 行しか見えず、
    項目の名前も切れた)。fixed_height なら高さを固定し、そうでなければ希望の大きさにする(_FullTable)。"""
    table.resizeColumnsToContents()
    table.resizeRowsToContents()
    frame = 2 * table.frameWidth()
    width = sum(table.columnWidth(c) for c in range(table.columnCount())) + frame
    height = table.horizontalHeader().sizeHint().height() + sum(table.rowHeight(r) for r in range(table.rowCount()))
    height += frame
    if fixed_height:
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.setMinimumWidth(width)
        table.setFixedHeight(height)
    elif isinstance(table, _FullTable):
        table.full_size = QSize(width, height)
        table.updateGeometry()
