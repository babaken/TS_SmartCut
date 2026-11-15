"""録画の番組情報(broadcast/program_info.py)を表示し、rplstool-cs の CLI と照合する(自動カット A1-2)。

使い方:
    python tools/check_program_info.py <録画.ts> [...]

rplstool-cs の CLI(`../rplstool-cs/RplsTool.Cli/bin/*/net8.0/RplsTool.Cli.dll`)と dotnet が
あれば、局名・番組名・説明・詳しい説明・放送開始時刻・長さを照合する(CLI はジャンルを
出さないので、ジャンルは表示だけ)。CLI はファイルの中ほどの「現在の番組」を読むので、
照合する相手は、こちらが選んだ番組。

照合に失敗した録画があれば終了コード 1。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ts_smartcut.broadcast.program_info import ProgramInfo, read_program_info  # noqa: E402

RPLSTOOL = Path(__file__).resolve().parents[2] / "rplstool-cs"


def find_cli() -> Optional[Path]:
    for dll in sorted((RPLSTOOL / "RplsTool.Cli" / "bin").glob("*/net*/RplsTool.Cli.dll")):
        return dll
    return None


def run_cli(dll: Path, ts: Path) -> Optional[Dict[str, str]]:
    """CLI の出力を {"Rec": …, "Dur": …, "Channel": …, "Program": …, "Detail": …, "Extend": …} にする。"""
    env = dict(os.environ, DOTNET_ROLL_FORWARD="Major")  # net8.0 の CLI を新しい .NET で動かす
    if os.name == "nt":
        # .NET は出力先がパイプの時もコンソールのコードページ(cp932)で書くので UTF-8 に切り替える。
        # 見えない別のコンソールで動かし、この端末のコードページは変えない
        command = f'cmd /c "chcp 65001 >nul && dotnet "{dll}" "{ts.resolve()}""'
        result = subprocess.run(command, capture_output=True, env=env, timeout=120,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        result = subprocess.run(["dotnet", str(dll), str(ts)], capture_output=True, env=env, timeout=120)
    if result.returncode != 0:
        return None
    lines = result.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n").split("\n")
    fields: Dict[str, str] = {}
    block: Optional[str] = None
    block_lines: List[str] = []
    for line in lines:
        if line in ("Detail:", "Extend:"):
            if block:
                fields[block] = "\n".join(block_lines).rstrip("\n")
            block, block_lines = line[:-1], []
        elif block:
            block_lines.append(line)
        elif ": " in line:
            key, value = line.split(": ", 1)
            fields[key] = value
    if block:
        fields[block] = "\n".join(block_lines).rstrip("\n")
    return fields


def expected_from(info: ProgramInfo) -> Dict[str, str]:
    """こちらの結果を、CLI と同じ形の文字列にする。"""
    event = info.event
    fields = {"Channel": info.service_name}
    if event is not None:
        if event.start is not None:
            fields["Rec"] = event.start.strftime("%Y/%m/%d %H:%M:%S")
        if event.duration is not None:
            seconds = int(event.duration.total_seconds())
            fields["Dur"] = f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
        fields["Program"] = event.name
        fields["Detail"] = event.description
        if event.extended.strip():
            fields["Extend"] = event.extended
    return fields


def compare(info: ProgramInfo, cli: Dict[str, str]) -> List[str]:
    """一致しない項目の説明。CLI の Channel は「局名 (123ch)」なので局名の部分だけ比べる。"""
    ours = expected_from(info)
    theirs = dict(cli)
    if "Channel" in theirs:
        theirs["Channel"] = theirs["Channel"].rsplit(" (", 1)[0]
    problems = []
    for key in ("Channel", "Rec", "Dur", "Program", "Detail", "Extend"):
        if ours.get(key, "") != theirs.get(key, ""):
            problems.append(f"{key}: こちら={ours.get(key, '')!r} CLI={theirs.get(key, '')!r}")
    return problems


def describe(info: ProgramInfo) -> str:
    lines = [
        f"局: {info.service_name}(事業者: {info.provider_name})  {info.broadcast_type}  "
        f"service_id=0x{info.service_id:04X} original_network_id="
        + (f"0x{info.original_network_id:04X}" if info.original_network_id is not None else "不明"),
        f"録画: {info.recording_start} 〜 {info.recording_end}",
        "録画の中の番組:",
    ]
    for event in info.events:
        mark = "*" if event is info.event else " "
        genres = ",".join(f"0x{g:02X}" for g in event.genres)
        lines.append(f" {mark} {event.start} {event.duration} [{genres}] {event.name}")
    if info.event is not None:
        lines += ["説明:", info.event.description, "詳しい説明:", info.event.extended]
    lines.append("ES: " + ", ".join(
        f"PID 0x{s.pid:04X} type 0x{s.stream_type:02X}" for s in info.streams))
    return "\n".join(lines)


def main(argv: List[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if len(argv) < 2:
        print(__doc__)
        return 2
    dll = find_cli()
    failed = False
    for name in argv[1:]:
        ts = Path(name)
        print(f"=== {ts.name}")
        info = read_program_info(ts)
        if info is None:
            print("番組情報が見つかりません")
            failed = True
            continue
        print(describe(info))
        if dll is None:
            print("(rplstool-cs の CLI が無いので照合しません)")
            continue
        cli = run_cli(dll, ts)
        if cli is None:
            print("CLI の実行に失敗しました")
            failed = True
            continue
        problems = compare(info, cli)
        if problems:
            failed = True
            print("CLI と不一致:")
            print("\n".join("  " + p for p in problems))
        else:
            print("CLI と一致(局名・番組名・説明・詳しい説明・放送開始時刻・長さ)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
