# TS_SmartCut Windows 実行環境のセットアップ手順

[English](TS_SmartCut_Windows_Setup.md) | 日本語

対象: Windows PC で TS_SmartCut をソースから動かすための準備（アプリを使うだけの環境）。
.exe にして配る時は [TS_SmartCut_Packaging_jp.md](TS_SmartCut_Packaging_jp.md) の「2. Windows — PyInstallerでの.exe化」を参照
（.exe なら Python と仮想環境は要らず、下の 1 の 2・3（ffmpeg と mpv）だけを入れる）。

============================================================
## 1. 事前に用意するもの
============================================================
1. **Python 3.10 以上**（公式インストーラ、"Add python.exe to PATH" を有効にする。3.14.7 で動作確認済み）
2. **ffmpeg / ffprobe**（6 以降。公式ビルドを展開し、`ffmpeg.exe`/`ffprobe.exe` のあるフォルダを PATH に
   追加する。chocolatey 等でも可）
3. **mpv と libmpv（`libmpv-2.dll`）** — 下の「1.1 mpv の入れ方」の手順で入れる。
   アプリは `libmpv-2.dll`（または `mpv-2.dll`）を PATH（と実行ファイルの隣）から探して読み込むので、
   **`mpv.exe` だけでは足りない**。chocolatey 版・winget 版（`mpv-player.mpv-CI.MSVC`）の
   mpv にはこの DLL が入っておらず、読み込みが OSError で失敗した（2026-08、実機。当時は python-mpv の `import mpv`）。
4. **Git for Windows**（ソースの取得と更新に使う）

### 1.1 mpv の入れ方（shinchiro 氏のビルド）

shinchiro 氏の Windows 向けビルド（GitHub の `shinchiro/mpv-winbuild-cmake` の Releases）から、
**同じ日付の 2 つの 7z** を取る。

| ファイル | 中身 | 使うもの |
|---|---|---|
| `mpv-x86_64-v3-<日付>-git-<ハッシュ>.7z` | プレイヤー本体 | `mpv.exe`、`mpv.com`、`d3dcompiler_43.dll` など |
| `mpv-dev-x86_64-v3-<日付>-git-<ハッシュ>.7z` | 開発用（libmpv） | **`libmpv-2.dll`**（アプリが読み込む本体） |

- `x86_64-v3` は AVX2 のある CPU 向け（Intel 第 4 世代 Haswell 以降、AMD Zen 以降）。古い CPU では
  `-v3` の付かない `mpv-x86_64-…` と `mpv-dev-x86_64-…` を使う。
- 2 つの 7z を **同じフォルダ** に展開し（例: `C:\Tools\mpv\`）、そのフォルダを PATH に追加する。
  winget の mpv のフォルダに上書きして展開しても動くが、winget で mpv を更新すると上書きされて DLL が
  消えることがあるので、自分のフォルダの方がよい（動作確認時のビルド: v0.41.0-920-gdd5d17d32、2026-08-09）。
- 確かめ方（新しい PowerShell で、PATH を読み直してから）:

  ```powershell
  where.exe libmpv-2.dll     # 展開したフォルダの libmpv-2.dll が出ること
  mpv --version              # 展開したビルドの版が出ること
  ```

============================================================
## 2. ソースの取得
============================================================
GitHub のリポジトリ（`github.com/babaken/TS_SmartCut`、ブランチ `main`）から clone する:

```powershell
git clone https://github.com/babaken/TS_SmartCut.git
cd TS_SmartCut
```

============================================================
## 3. 仮想環境と依存パッケージ（PowerShell）
============================================================
```powershell
# 仮想環境の作成
python -m venv .venv
.venv\Scripts\Activate.ps1

# 依存パッケージ（PySide6。libmpv は自作の結び付けで読む（2026-11-15 から python-mpv は不要）。自動カットの計算と学習は Rust の部品で行う（2026-10-30 から LightGBM・
# numpy は不要。テストと確かめのツールを動かす時だけ `pip install numpy pytest`））
pip install -r requirements.txt

# 確かめ
python -c "import PySide6; print('imports OK')"
ffmpeg -version
ffprobe -version
```

自動カットの計算の Rust の部分（`native/`。**必須**、2026-10-28。無いと自動カット（特徴の計算・案・自動補正・学習・ロゴ）と
音声波形が使えない — 起動の後に E506 の知らせが出る。カットの編集・変換はできる）: Rust を入れて（`winget install Rustlang.Rustup`
か https://rustup.rs/ 。C のリンカーとして Visual Studio Build Tools の「C++ によるデスクトップ開発」が要る）、仮想環境で

```powershell
pip install .\native
python -c "import ts_smartcut_native; print(ts_smartcut_native.VERSION)"
```

Python で計算していた頃より、特徴の計算は約 2.5 倍、バッチの後のしきい値の自動補正は約 6 倍速い（どちらも Windows で測定）。
Python の同じ処理は 2026-10-28 に削ったので、入れずに使うことはできない。
`native/` が変わったら（`git pull` で変わった時も）同じコマンドで入れ直す。

============================================================
## 4. 起動
============================================================
普段は `scripts\run_ts_smartcut.bat` で起動する（エクスプローラーでダブルクリック。デスクトップや
スタートメニューにショートカットを作ってもよい）。

- プロジェクトの `.venv` の `pythonw.exe` で起動するので、仮想環境の有効化は要らず、コンソールの黒い窓も出ない。
- どこから起動しても、作業フォルダをプロジェクト直下に移してから起動する。
- 起動しない時は `scripts\run_ts_smartcut.bat /console` で実行すると、エラーがコンソールに出る。

設定（`settings.json`）・自動カットの記録 DB（`cut_history.db`）・ログ（`logs\`）・一時ファイル（`Temp\`）・
キーフレームのキャッシュ（`KeyframeCache\`）・局ロゴ（`Logo\`）は、プロジェクト直下に作られる。
書き込めないフォルダに置くと起動しない（E505）。

============================================================
## 5. 更新
============================================================
PowerShell で:

```powershell
cd TS_SmartCut
.venv\Scripts\Activate.ps1
git pull
pip install -r requirements.txt   # 依存パッケージが変わった時のため
pip install .\native              # git pull の表示に native/ があった時（下の確かめで合わないと出た時も）
```

`native\` の作り直しには数分かかる。動いているアプリは古い部品のまま動くので、終わってから起動し直す。

### 確かめ

```powershell
python -c "from ts_smartcut.autocut.native import native_problem as p; print(p() or 'native OK')"
ffmpeg -version                    # 6 以上
```

- `native OK` と出れば Rust の部品がアプリと合っている。合わない時は入っている版と必要な版が出るので、`pip install .\native` で入れ直す。
- アプリを起動し直し、起動の後に E506 の知らせが出ないこと、「ヘルプ → システム情報」に ffmpeg・mpv・`ts_smartcut_native` の版が出ることを見る。
- 録画を 1 本開いて自動カットの案が出ること、短い区間をバッチで変換できることを見れば、普段の使い方は確かめられる。
- アプリ全体を確かめる時はテストを動かす（実物の ffmpeg・mpv・Qt を使う。10 分ほど）:

```powershell
pip install -e ".[dev]"
python -m pytest -q -p no:faulthandler
```

============================================================
## 6. うまく動かない時
============================================================
- **プレビューが出ない（「mpv: 利用不可（プレビュー制限モード）」と出る）**: まず `where.exe libmpv-2.dll` を確かめる。アプリは mpv が無くても
  起動し、カット編集はできる。
- **起動しない**: `scripts\run_ts_smartcut.bat /console` でエラーを見る。ログは `logs\ts_smartcut.log`。
- **終了時にまれに落ちる**: Windows では終了時にまれに（数 % 程度）クラッシュすることがある（libmpv 側の
  問題と考えられる）。終了時だけで、編集中やエンコード中には起きない。
