"""残す区間の追加・編集・削除とUndo/Redo (design doc §7)。

self.cutsは常にstart_frame順にソートされているため、Undo/Redoの
対象操作を「位置インデックス」ではなく「値(CutRange)」で同定する
（ソート後にインデックスが変わっても安全に戻せるようにするため。
design doc本文はこの部分を省略しているため、ここで具体化した）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ts_smartcut.errors import ValidationError
from ts_smartcut.models import (
    PROPOSAL_ACCEPTED,
    PROPOSAL_PENDING,
    PROPOSAL_REJECTED,
    CutProposal,
    CutRange,
    FileCutInfo,
    OperationHistory,
)

if TYPE_CHECKING:
    from ts_smartcut.core.bookmark_manager import BookmarkManager

MAX_HISTORY = 20


class CutManager:
    def __init__(self, file_info: FileCutInfo):
        self.file_info = file_info
        self.cuts: List[CutRange] = []
        self.history: List[OperationHistory] = []
        self.history_index: int = -1
        # 自動カット用の編集イベント履歴(累積ログ、A2)
        self.edit_events: List[Dict[str, Any]] = []
        # 自動カットの案(A6)。承認・棄却は Undo/Redo できる
        self.proposals: List[CutProposal] = []

    @property
    def can_undo(self) -> bool:
        return self.history_index >= 0

    @property
    def can_redo(self) -> bool:
        return self.history_index < len(self.history) - 1

    @property
    def total_cut_frames(self) -> int:
        """残す区間すべてのフレーム数の合計(終了フレームは含まない)。区間は重ならない
        (add_cut/edit_cutが重複を拒否する)ので単純な合計でよい (design doc §4.8)。"""
        return sum(cut.end_frame - cut.start_frame for cut in self.cuts)

    @property
    def total_proposal_frames(self) -> Optional[int]:
        """自動カットが出した案すべてのフレーム数の合計(承認・棄却しても変わらない)。
        案が無ければ None。見出しの「計算再生時間」(2026-10-11)。"""
        if not self.proposals:
            return None
        return sum(p.end_frame - p.start_frame for p in self.proposals)

    def _record_event(self, event_type: str, details: dict) -> None:
        self.edit_events.append({
            "event_order": len(self.edit_events),
            "event_type": event_type,
            "details": details,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })

    def add_cut(self, start: int, end: int, label: Optional[str] = None, is_excluded: bool = False) -> bool:
        """残す区間を追加する（履歴付き）。"""
        self._validate_range(start, end)
        if self._check_overlap(start, end):
            raise ValidationError("E202", key="exceptions.cut_overlap")

        cut = CutRange(start, end, label, is_excluded=is_excluded)
        self._insert(cut)
        self._record_history("add_cut", {"cut": cut})
        self._record_event("add_cut", {
            "start_frame": start,
            "end_frame": end,
            "label": label,
            "is_excluded": cut.is_excluded,
        })
        return True

    def remove_cut(self, index: int) -> bool:
        """残す区間を削除する。"""
        if not (0 <= index < len(self.cuts)):
            return False
        cut = self.cuts.pop(index)
        self._record_history("remove_cut", {"cut": cut})
        self._record_event("remove_cut", {
            "index": index,
            "start_frame": cut.start_frame,
            "end_frame": cut.end_frame,
            "label": cut.label,
        })
        return True

    def edit_cut(self, index: int, start: int, end: int) -> bool:
        """残す区間を編集する。"""
        if not (0 <= index < len(self.cuts)):
            return False

        old_cut = self.cuts[index]
        self._validate_range(start, end)

        for cut in self.cuts[:index] + self.cuts[index + 1 :]:
            if self._ranges_overlap(start, end, cut.start_frame, cut.end_frame):
                raise ValidationError("E202", key="exceptions.cut_overlap")

        new_cut = CutRange(start, end, old_cut.label, is_excluded=old_cut.is_excluded)
        self.cuts[index] = new_cut
        self.cuts.sort(key=lambda c: c.start_frame)

        self._record_history("edit_cut", {"old": old_cut, "new": new_cut})
        self._record_event("edit_cut", {
            "index": index,
            "old_start": old_cut.start_frame,
            "old_end": old_cut.end_frame,
            "new_start": new_cut.start_frame,
            "new_end": new_cut.end_frame,
        })
        return True

    # ---- 自動カットの案 (設計書 §31.6、計画 A6) ---------------------------------

    @property
    def pending_proposals(self) -> List[CutProposal]:
        return [p for p in self.proposals if p.status == PROPOSAL_PENDING]

    def set_proposals(self, proposals: List[CutProposal]) -> None:
        """案を入れ替える(履歴には残さない)。すでにある残す区間と重なる案は出さない
        (プロジェクトを開き直した時など、もう決めてある所)。"""
        self.proposals = [p for p in proposals if not self._check_overlap(p.start_frame, p.end_frame)]
        self._record_event("proposals_shown", {
            "proposals": [p.to_dict() for p in self.proposals],
        })

    def accept_proposal(self, proposal: CutProposal) -> CutRange:
        """案を残す区間にする。他の残す区間と重なる時は E202。"""
        cut = self._accept(proposal)
        self._record_history("accept_proposals", {"items": [(proposal, cut)]})
        return cut

    def accept_all_proposals(self) -> List[CutProposal]:
        """未判断の案をすべて承認する(1回の Undo で戻る)。他の残す区間と重なって承認
        できなかった案を返す。"""
        items, skipped = [], []
        for proposal in self.pending_proposals:
            try:
                items.append((proposal, self._accept(proposal)))
            except ValidationError:
                skipped.append(proposal)
        if items:
            self._record_history("accept_proposals", {"items": items})
        return skipped

    def reject_proposal(self, proposal: CutProposal) -> None:
        """案を捨てる(そこは本編ではないと判断した。棄却は学習のデータになる)。"""
        if proposal.status != PROPOSAL_PENDING:
            return
        proposal.status = PROPOSAL_REJECTED
        self._record_history("reject_proposal", {"proposal": proposal})
        self._record_event("proposal_rejected", proposal.to_dict())

    def _accept(self, proposal: CutProposal) -> CutRange:
        if proposal.status != PROPOSAL_PENDING:
            raise ValidationError("E201", key="exceptions.proposal_decided")
        self._validate_range(proposal.start_frame, proposal.end_frame)
        if self._check_overlap(proposal.start_frame, proposal.end_frame):
            raise ValidationError("E202", key="exceptions.cut_overlap")
        cut = CutRange(proposal.start_frame, proposal.end_frame)
        self._insert(cut)
        proposal.status = PROPOSAL_ACCEPTED
        self._record_event("proposal_accepted", proposal.to_dict())
        return cut

    def toggle_excluded(self, index: int) -> bool:
        """カット区間の「基準外」フラグを切り替える(設計書 §31.8-1、A2)。"""
        if not (0 <= index < len(self.cuts)):
            return False
        cut = self.cuts[index]
        old_val = cut.is_excluded
        cut.is_excluded = not old_val
        self._record_history("toggle_excluded", {"cut": cut, "old": old_val, "new": cut.is_excluded})
        self._record_event("toggle_excluded", {
            "index": index,
            "start_frame": cut.start_frame,
            "end_frame": cut.end_frame,
            "is_excluded": cut.is_excluded,
        })
        return True

    def clear_all(self, bookmarks: Optional["BookmarkManager"] = None) -> bool:
        """残す区間と(渡されれば)ブックマークをすべて消す (design doc §4.2「すべてクリア」)。
        1回のUndoで両方とも戻せる。消すものが無ければFalse。"""
        saved_bookmarks = list(bookmarks.bookmarks) if bookmarks is not None else []
        if not self.cuts and not saved_bookmarks:
            return False
        saved_cuts = list(self.cuts)
        self.cuts.clear()
        if bookmarks is not None:
            bookmarks.clear()
        self._record_history(
            "clear_all", {"cuts": saved_cuts, "bookmarks": saved_bookmarks, "bookmark_manager": bookmarks}
        )
        self._record_event("clear_all", {
            "cuts_count": len(saved_cuts),
            "bookmarks_count": len(saved_bookmarks),
        })
        return True

    def undo(self) -> bool:
        """直前の操作を取り消す。"""
        if not self.can_undo:
            return False
        op = self.history[self.history_index]
        self._revert_operation(op)
        self.history_index -= 1
        self._record_event("undo", {"reverted_op": op.operation_type})
        return True

    def redo(self) -> bool:
        """取り消した操作をやり直す。"""
        if not self.can_redo:
            return False
        self.history_index += 1
        op = self.history[self.history_index]
        self._apply_operation(op)
        self._record_event("redo", {"applied_op": op.operation_type})
        return True

    # ---- 内部ヘルパー -------------------------------------------------

    def _validate_range(self, start: int, end: int) -> None:
        if start < 0 or end > self.file_info.total_frames:
            raise ValidationError("E203", key="exceptions.frame_out_of_range")
        if start >= end:
            raise ValidationError("E204", key="exceptions.start_not_before_end")

    def _check_overlap(self, start: int, end: int) -> bool:
        return any(
            self._ranges_overlap(start, end, cut.start_frame, cut.end_frame)
            for cut in self.cuts
        )

    @staticmethod
    def _ranges_overlap(s1: int, e1: int, s2: int, e2: int) -> bool:
        return not (e1 <= s2 or e2 <= s1)

    def _insert(self, cut: CutRange) -> None:
        self.cuts.append(cut)
        self.cuts.sort(key=lambda c: c.start_frame)

    def _remove_value(self, cut: CutRange) -> None:
        self.cuts.remove(cut)

    def _record_history(self, op_type: str, data: dict) -> None:
        self.history = self.history[: self.history_index + 1]
        self.history.append(OperationHistory(op_type, data))
        self.history_index += 1

        if len(self.history) > MAX_HISTORY:
            self.history.pop(0)
            self.history_index -= 1

    def _revert_operation(self, op: OperationHistory) -> None:
        if op.operation_type == "add_cut":
            self._remove_value(op.data["cut"])
        elif op.operation_type == "remove_cut":
            self._insert(op.data["cut"])
        elif op.operation_type == "edit_cut":
            self._remove_value(op.data["new"])
            self._insert(op.data["old"])
        elif op.operation_type == "toggle_excluded":
            op.data["cut"].is_excluded = op.data["old"]
        elif op.operation_type == "accept_proposals":
            for proposal, cut in op.data["items"]:
                self._remove_value(cut)
                proposal.status = PROPOSAL_PENDING
        elif op.operation_type == "reject_proposal":
            op.data["proposal"].status = PROPOSAL_PENDING
        elif op.operation_type == "clear_all":
            for cut in op.data["cuts"]:
                self._insert(cut)
            manager = op.data["bookmark_manager"]
            if manager is not None:
                # ブックマークの追加・削除は履歴に残らないので、クリア後に足されたものは残す
                manager.bookmarks.extend(op.data["bookmarks"])
                manager.bookmarks.sort(key=lambda b: b.frame)

    def _apply_operation(self, op: OperationHistory) -> None:
        if op.operation_type == "add_cut":
            self._insert(op.data["cut"])
        elif op.operation_type == "remove_cut":
            self._remove_value(op.data["cut"])
        elif op.operation_type == "edit_cut":
            self._remove_value(op.data["old"])
            self._insert(op.data["new"])
        elif op.operation_type == "toggle_excluded":
            op.data["cut"].is_excluded = op.data["new"]
        elif op.operation_type == "accept_proposals":
            for proposal, cut in op.data["items"]:
                self._insert(cut)
                proposal.status = PROPOSAL_ACCEPTED
        elif op.operation_type == "reject_proposal":
            op.data["proposal"].status = PROPOSAL_REJECTED
        elif op.operation_type == "clear_all":
            for cut in op.data["cuts"]:
                self._remove_value(cut)
            manager = op.data["bookmark_manager"]
            if manager is not None:
                for bookmark in op.data["bookmarks"]:
                    if bookmark in manager.bookmarks:
                        manager.bookmarks.remove(bookmark)
