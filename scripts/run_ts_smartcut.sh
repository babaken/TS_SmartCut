#!/usr/bin/env bash
# TS_SmartCut起動ラッパー。デスクトップショートカット(.desktop)から呼ばれる。
# プロジェクトルートの.venv内のPythonでts_smartcut.mainを起動する。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# `python -m ts_smartcut.main`はCWDがプロジェクトルートであることに暗黙に
# 依存する(-mはCWDをsys.pathへ追加する)。KDE Plasma 6のKickoff等はアプリを
# systemd-run --user --scope経由で起動し、その際のCWDはプロジェクトルートに
# ならない(ModuleNotFoundError: No module named 'ts_smartcut'で起動失敗する
# ことを実機で確認済み)。cdで明示的に固定し、呼び出し元のCWDに依存しない
# ようにする。
cd "$PROJECT_ROOT"

exec "$PROJECT_ROOT/.venv/bin/python" -m ts_smartcut.main "$@"
