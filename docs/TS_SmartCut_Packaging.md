# TS_SmartCut パッケージ化手順

対象: Phase 0〜10完了後のTS_SmartCutを、Linuxではアプリランチャー
ショートカットとして、Windowsでは配布可能な.exeとして配布する手順。

外部ツール（ffmpeg/ffprobe/mpv）は同梱せず、実行環境に別途インストールして
PATHに通しておく前提（判断: 2026-08-21）。同梱する場合はライセンス文書の
添付が必要になる点に注意（ffmpeg/mpvはビルドによりLGPL/GPL）。

============================================================
## 1. Linux — デスクトップランチャー
============================================================

以下の2ファイルを作成済み:

- `scripts/run_ts_smartcut.sh` — プロジェクトの`.venv`内Pythonで
  `ts_smartcut.main`を起動するラッパースクリプト。`cd`でプロジェクト
  ルートに明示的に移動してから`python -m ts_smartcut.main`を実行する
  （KDE Plasma 6のKickoff等、`systemd-run --user --scope`経由で起動される
  場合はCWDがプロジェクトルートにならず`ModuleNotFoundError`になることを
  実機で確認したため、CWD非依存にした）。
- `~/.local/share/applications/ts_smartcut.desktop` — アプリケーション
  メニューに表示される起動エントリ（`Exec=`が上記ラッパーを指す。
  `Path=`もプロジェクトルートを明示、`Icon=`は下記アイコン参照）

再セットアップが必要な場合:
```bash
chmod +x scripts/run_ts_smartcut.sh
# ts_smartcut.desktop はリポジトリに無いので、別環境では作る（<repo> はプロジェクトの絶対パス）
cat > ~/.local/share/applications/ts_smartcut.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=TS_SmartCut
Exec=<repo>/scripts/run_ts_smartcut.sh
Path=<repo>
Icon=<repo>/ts_smartcut/gui/resources/icon.png
Terminal=false
Categories=AudioVideo;Video;
EOF
update-desktop-database ~/.local/share/applications   # メニューキャッシュ更新
kbuildsycoca6 --noincremental                          # KDEのアプリ一覧キャッシュ更新（KDE環境の場合）
```

### アイコン（1ファイルに統一）

アイコンの実体は`ts_smartcut/gui/resources/icon.png`（プロジェクト同梱、
Gitで管理される唯一のソース）**1つだけ**。以下2箇所がこの同じファイルを
指す構成にしている（コピーは作らない）:

- `.desktop`の`Icon=`は絶対パスでこのファイルを直接指す
  （`Icon=/path/to/TS_SmartCut/ts_smartcut/gui/resources/icon.png`）
  → Kickoff等のアプリ一覧に表示される
- `ts_smartcut/main.py`の`app.setWindowIcon(QIcon(...))`も同じファイルを
  読み込む → 実行中ウィンドウ・タスクバーのアイコン

差し替える場合は`ts_smartcut/gui/resources/icon.png`を上書きするだけで
両方に反映される（`.desktop`側は絶対パス参照なので再配置不要。ただし
KDEのアプリ一覧サムネイルはキャッシュされるため、見た目が変わらない
場合は`kbuildsycoca6 --noincremental`でキャッシュ更新する）。

Windows（2026-10-06）:
- タスクバー: `pythonw.exe`で起動するとタスクバーにPythonのアイコンが出ていた
  （Windowsはプロセスを起動元の実行ファイルの仲間として扱う）。`main.py`が
  ウィンドウを作る前に`SetCurrentProcessExplicitAppUserModelID("babaken.TS_SmartCut")`
  を呼び、`setWindowIcon()`のアイコンが出るようにした（画面を撮って確認済み）。
- .exe: `ts_smartcut/gui/resources/icon.ico`を`ts_smartcut.spec`の`icon=`に指定。
  `.ico`は`icon.png`から`tools/make_icon.py`で作る（16〜256pxのPNGを入れたICO、
  Pillow不要）。`icon.png`を差し替えたら`python tools/make_icon.py`も実行し直す。

なお、Wayland環境ではタスクバー/ウィンドウ切替のアイコンは`.desktop`の
`Icon=`だけでは反映されず、アプリ自身がQtの`app.setDesktopFileName()`で
自分の`.desktop`ファイル名（拡張子抜き）と紐付ける必要があった
（`main.py`で対応済み。実機で「メニューのアイコンは出るが実行中の
タスクバーアイコンは汎用フォールバックのまま」という食い違いを確認して
対処した）。

動作確認: `scripts/run_ts_smartcut.sh`を直接実行し、ウィンドウが起動し
ログ（当時は`~/.config/ts_smartcut/logs/ts_smartcut.log`。2026-10-12 からはプロジェクト直下の
`logs/ts_smartcut.log`）に`mpvへ接続しました`（2026-10-13 からログは英語で`Connected to mpv`）が出ること、スクリーンショットでタスクバーに
アイコンが表示されることを確認済み。

### Windows — 起動バッチ（.exe化しない場合）
`scripts/run_ts_smartcut.bat` — `run_ts_smartcut.sh`のWindows版。プロジェクト
直下に`cd`してから、`.venv\Scripts\pythonw.exe -m ts_smartcut.main`を
`start`で起動する（コンソール窓なし）。`/console`を付けると`python.exe`で
起動してコンソールにエラーを表示し、終了後に`pause`する。`.venv`が無ければ
その旨を表示する。cmd.exeは.batをANSIコードページ(cp932)で読むので、中身は
ASCIIのみにしてある。改行はCRLF（`.gitattributes`で固定）。動作確認
(2026-10-05、Windows 11): 別フォルダから実行してウィンドウが開き、ログに
`TS_SmartCut起動`・`mpvへ接続しました`が出ることを確認（今のログは英語で`TS_SmartCut started`・`Connected to mpv`）。

============================================================
## 2. Windows — PyInstallerでの.exe化
============================================================

### 2.1 使うファイル
`ts_smartcut.spec`（プロジェクトルート）— Linux/Windows共通のビルド定義。
`--onedir`形式（フォルダ配布。起動が速く、PySide6公式にも推奨される形式。
`--onefile`は起動のたびに一時展開が挟まり数秒遅くなる上、Qtアプリでは
不安定という報告もあるため採用しない）。

### 2.2 事前準備（Windows側、PowerShell）
`TS_SmartCut_Windows_Setup.md`の手順でソースから動く状態が前提。
それに加えて:
```powershell
cd C:\path\to\TS_SmartCut
.venv\Scripts\Activate.ps1
pip install -e ".[build]"     # pyinstaller>=6.0 を追加インストール
```

### 2.3 ビルド
```powershell
pyinstaller ts_smartcut.spec --noconfirm
```
出力: `dist\TS_SmartCut\`フォルダ（中に`TS_SmartCut.exe`と`_internal\`）。
このフォルダごとZIP圧縮して配布する。

使っていない Qt の部品は `ts_smartcut.spec` で外す（2026-10-28）。アプリが使うのは QtCore・QtGui・QtWidgets・QtOpenGL・
QtOpenGLWidgets・QtSvg だけで、PySide6 のフックが拾う QML・Quick・仮想キーボード・PDF・ネットワーク・TLS・Qt の翻訳は
入れない（`_UNUSED_QT`）。ソフトウェアの OpenGL（`opengl32sw.dll`）は、リモートデスクトップなど GPU の OpenGL が無い時の
プレビューの予備として残す。フォルダは約 239 MB → 約 211 MB、ファイルは 360 → 243 個（Rust の部品と LightGBM を入れた形。
残りの大きいものは PySide6 約 75 MB・LightGBM が使う scipy 約 67 MB・numpy 約 28 MB。2026-10-30 に学習を Rust にして
LightGBM と scipy は使わなくなった（設計書 §31.9「学習を Rust で（④）」）。同じ日に numpy も使わなくなった（「numpy を
やめる（⑤）」）。`ts_smartcut.spec` の `excludes` で numpy・lightgbm・scipy を外す（作る PC の仮想環境にテスト用の numpy が
入っていても同梱しない）。作って測ると、学習を Rust にした後 約 137 MB、numpy も外した後 約 108 MB（2026-10-30、`_internal\` に numpy・scipy・lightgbm は
無い。大きいものは PySide6 約 75 MB・python314.dll 約 7 MB・libcrypto 約 12 MB））。外した形で固めたプログラムで、
起動・mpv への接続・録画を開いてプレビューに絵が出る・設定の画面（6 つのタブ）・システム情報・スタイル（windows11）を
確かめた。Qt の部品を新しく使う時（`import PySide6.Qt…` を足す時）は、`_UNUSED_QT` に入っていないかを見る。

OpenSSL も入れない（2026-11-15）。アプリは通信しないので、`excludes` で `ssl`・`_ssl`・`_hashlib` を外す。`hashlib` は Python
本体の sha1・sha256 を使う（OpenSSL の時と同じ値。キーフレームのキャッシュの名前と記録のハッシュ）。これで Python の
`libcrypto-3.dll`・`libssl-3.dll` が入らない。Qt の TLS の部品が求める `libcrypto-3-x64.dll`・`libssl-3-x64.dll` は、PATH の上の
別のソフト（Git など）のものを拾っていて、.exe の中では何も読み込んでいなかったので、`_UNUSED_QT` で外す。フォルダは
107 MB → 94 MB、ファイルは 117 → 111 個（Python 3.14.10・PySide6 6.11.2）。外した形で起動・mpv への接続を確かめた。
Python 3.15 は今は使わない（2026-11-15）: 3.15 で使える PySide6 6.12.0 は `builtins.__import__` を置き換えるため、3.15 の
遅延 import（`lazy from …`）が中身に替わらず、`concurrent.futures.ThreadPoolExecutor` が呼べない（自動補正の並列が失敗する。
`tests/test_native.py` の 2 件）。PySide6 の新しい版が出たら、テストと .exe で確かめ直す。

### 2.4 動作確認
```powershell
cd dist\TS_SmartCut
.\TS_SmartCut.exe
```
- ウィンドウが起動し、メニュー・カット編集操作ができるか
- 動画を開いてプレビューが実描画されるか（mpv-2.dll/libmpv-2.dllが
  PATH上にあることが前提。無い場合はTS_SmartCut_Windows_Setup.md §1.1の
  手順でインストールしたビルドのフォルダをPATHに追加する）
- 設定・記録 DB・ログが`.exe`と同じフォルダ（`settings.json`・`cut_history.db`・`logs\`）に正しく作られるか
  （2026-10-12 までは`%APPDATA%\ts_smartcut\`。Program Files など書き込めない場所では起動しない）
- 日本語/英語メニューがどちらも文字化けなく表示されるか
  （`_internal\ts_smartcut\i18n\languages\*.json`が同梱されていることを
  Linux側の事前検証で確認済み。`language_manager.py`は`sys.frozen`時に
  `sys._MEIPASS`基準でこのパスを解決するよう対応済み）
- 自動カットの学習（A8、2026-10-26。2026-10-30 から Rust の部品で学習する）: 設定の「自動カット」→「学習」で
  「学習し直す」ができるか。`_internal\` に `lightgbm`・`scipy` が入っていないか（入っていれば作る PC の仮想環境から消す）
- 自動カットの計算の Rust の部分（`native/`、2026-10-27。**2026-10-28 から必須**）: ビルドする仮想環境に `pip install .\native`
  で入れておくと、`ts_smartcut_native` が同梱される（`ts_smartcut.spec` は直さなくてよい。`_internal\ts_smartcut_native\` に `.pyd`）。
  入れずに作ると、起動の後に E506 の知らせが出て、自動カット（特徴の計算・案・自動補正・学習・ロゴ）と音声波形が使えない。システム情報に「ts_smartcut_native <版>」(2026-10-31 から 8)と
  出るか、自動補正のログが「N threads」か。2026-10-28 に、入れて作った .exe と同じ環境で固めた確認用のプログラムで、
  特徴の計算（Rust で約 10 秒、当時の Python で約 30 秒。2.1 GB の録画）と自動補正（記録 DB の写し 314 本）が Rust で動き、
  Python と同じ値になることを確かめた

### 2.5 Linux側で事前検証済みの内容（今回のセッションで実施）
実際のWindows実行体はビルドできない（クロスコンパイル不可）ため、
`ts_smartcut.spec`をLinux上で一度ビルド・実行し、以下を確認済み:
- `pyinstaller ts_smartcut.spec`がエラーなく完走する
- PySide6（QtOpenGL/QtOpenGLWidgets含む）のhiddenimportsが過不足ないこと
- i18n JSON（`language_jp.json`/`language_en.json`）が`_internal/`配下に
  正しい相対構造で同梱され、frozen時のパス解決コード
  （`ts_smartcut/i18n/language_manager.py`）がそれを正しく見つけること
- ビルドした実行体を起動し、ログに`TS_SmartCut起動`
  `mpvへ接続しました`が記録され、6秒間クラッシュせず起動し続けること
  （今のログは英語で`TS_SmartCut started`・`Connected to mpv`）

Windows固有で追加確認が必要な項目（Linuxでは検証不能）:
- `mpv-2.dll`/`libmpv-2.dll`実体がPATH経由で正しく見つかるか
  （アプリ（`ts_smartcut/mpv/libmpv.py`、2026-11-15 までは python-mpv）はctypes経由でこのDLLを探すため、PyInstallerの静的解析では
  検出されず、`binaries=[]`のまま=未同梱。TS_SmartCut_Windows_Setup.md通り
  実行環境側にインストールしPATHに置く前提）
- Windows Defender等が未署名exeを誤検知しないか（PyInstaller製exeは
  誤検知されることがある。署名は現時点で未対応）
- GPUエンコーダ（QSV/NVENC）が.exe化後も`ffmpeg/gpu.py`から正しく検出されるか

### 2.6 既知の未対応項目
- コード署名なし。
- **Windows で .exe を作って 2.4 の確認を全部した記録は無い**（2026-10-17 時点）。2026-10-28 に、Rust の部分を入れた .exe が
  起動し、記録 DB を .exe の横に作って mpv につながることと、上の Rust の部分の確認はした。しきい値の自動補正の並列処理は
  2026-10-28 からスレッドだけ（`multiprocessing` の spawn と `main.py` の `freeze_support()` は削った）。配る前に 2.3・2.4 を行う。
- Linux の PyInstaller の出力も、2026-11-15 から配布物にする（下の 3）。Linux の配布物は、Linux の PC で作って確かめる。

## 3. 配布物（インストーラー）— `build_release.py`（2026-11-15）

PyInstaller（`ts_smartcut.spec`）の後に、Windows はインストーラー、Linux は tar.gz を作り、それぞれに SHA-256 の
`.sha256` を添える。版は `ts_smartcut/__init__.py` の `__version__`。README.md・README_jp.md・LICENSE・
THIRD_PARTY_NOTICES.md を実行ファイルの横に入れる（無ければ止める）。

```powershell
# PySide6・pyinstaller・ts_smartcut_native を入れた仮想環境で
python build_release.py            # Windows: dist\TS_SmartCut-<版>-setup.exe、Linux: dist/TS_SmartCut-<版>-linux64.tar.gz
python build_release.py --no-installer   # Windows: dist\TS_SmartCut まで
```

- 作る仮想環境には、テストした版の PySide6 を**最初から**入れる（`pip install "PySide6==<版>"`）。新しい版を入れた後で
  古い版に入れ替えると、部品が欠けて `DLL load failed while importing QtGui` で起動しない .exe ができた（2026-11-15）。
- **Windows**（`packaging/windows/TS_SmartCut.iss`、Inno Setup 6。ISCC.exe は `TS_SMARTCUT_ISCC`、
  `%LOCALAPPDATA%\Programs\Inno Setup 6`、`%ProgramFiles(x86)%\Inno Setup 6` の順に探す）
  - 利用者ごとに `%LOCALAPPDATA%\Programs\TS_SmartCut` へ入れる（管理者の権限は要らない）。設定・記録 DB・ログを
    実行ファイルの横に書くので、Program Files には入れない（書き込めず E505 で起動しない）。
  - 更新は、プログラムの部品（`_internal` は丸ごと入れ替え）だけを置き換え、設定・記録 DB・ログ・`Logo\` などは残す。
  - 削除は、設定・記録・ログ・キャッシュ・`Logo\` も消すかを尋ねる（既定は残す。黙って削除する時は残す）。
  - 最後に、ffmpeg・ffprobe・libmpv が PATH に無ければ知らせる。画面は日本語と英語。
  - 確かめたこと（2026-11-15、Windows 11）: 黙って入れる → 起動して mpv につながる → 同じ版で更新（設定と記録 DB は
    残り、古い版だけの部品は消える）→ 黙って削除（プログラム・スタートメニュー・アプリの一覧から消え、設定と記録は残る）。
    インストーラーは 27.3 MB、入れたフォルダは 94 MB。
- **Linux**（`packaging/linux/`）: tar.gz に `app/`（PyInstaller の出力）・`share/ts_smartcut.desktop`・`install.sh`・
  `uninstall.sh` を入れる（持ち主の名前は入れない）。
  - `sh install.sh`（root は要らない）で `${XDG_DATA_HOME:-~/.local/share}/TS_SmartCut` へ入れ、
    `~/.local/share/applications/ts_smartcut.desktop` を置く。更新は同じコマンドで、`_internal` と実行ファイルだけを
    入れ替える。最後に ffmpeg・ffprobe・libmpv（`ldconfig -p`）が無ければ知らせる。
  - `sh ~/.local/share/TS_SmartCut/uninstall.sh` で削除（設定と記録も消すかを尋ねる。`--purge` で尋ねずに消す。残した時は uninstall.sh も残り、後から `--purge` で消せる）。
  - 作る PC の部品を入れない（2026-11-15、Linux の確かめで見つかった）:
    - libmpv: `libmpv.py` は名前を `find_library()` の呼び出しに直接書かない（直接書くと PyInstaller が作る PC の libmpv と、
      その部品の ffmpeg・x264・x265・GTK などを丸ごと入れた。tar.gz が 146.9 MB）。spec は libmpv が入ると止める。
    - Qt の使わない部品は、Linux の場所（`PySide6/Qt/lib`・`plugins`・`translations`）でも外す。VNC の画面・TUIO のタッチの部品
      （Qt Network が要る）も外す。入力の部品は仮想キーボードだけ外す（ibus・compose は日本語の入力に要る）。
    - 部品を外すのは解析の後なので、外した部品だけが使っていたライブラリ（OpenSSL・Kerberos など）が残る。spec は、
      `_internal` の直下のライブラリのうち、残った部品から ldd の依存でたどれないものを外す（外した名前をビルドの出力に出す）。
    - Qt の GTK3 の見た目の部品（`platformthemes/libqgtk3.so`）は、作る PC の GTK3・ICU（約 42 MB）を引き込むので同梱せず、
      tar.gz の `optional/` に置く。`install.sh` は PC に GTK3（`libgtk-3.so.0`）がある時だけ入れ、PC の GTK3 を使う。
      無くてもアプリは動く（見た目とファイルを開く画面が GTK に合わないだけ）。GNOME の PC での確かめはまだ。
    - glib 一族（`libglib-2.0`・`libgobject-2.0`・`libgio-2.0`・`libgmodule-2.0`・`libgthread-2.0`）は同梱せず、いつも PC のものを
      使う。PC の libmpv（ffmpeg を通して gio・pango など）と GTK3 の見た目の部品が PC の gobject・gio を読み込むので、
      作った PC の glib を入れると版の違う glib と混ざる。
    - 同じ理由で、`libstdc++`・`libgcc_s`・`libfontconfig`・`libfreetype`・`libpcre2-8`・`libffi`・`libz`・`libexpat`・`libpng16` も
      同梱しない（`_SYSTEM_LIBRARIES`）。PyInstaller の実行ファイルは `_internal` を先に探すので、同梱した 24.04 の古い版が PC の
      新しい部品より先に読み込まれ、Arch では libmpv（PC の libopenal が新しい libstdc++ を求める）と GTK3 の見た目（PC の
      pango が新しい fontconfig を求める）が読めなかった（2026-11-15）。
    - 子のプロセス（ffmpeg・ffprobe、「フォルダを開く」のファイルマネージャーなど）には `_internal` を探させない: PyInstaller が
      書き換えた `LD_LIBRARY_PATH` を、`main()` の最初に `LD_LIBRARY_PATH_ORIG`（起動の前の値）に戻す
      （`child_processes.restore_library_path_for_children`。実行中のアプリ自身の探し場所は起動の時に決まるので変わらない）。
      戻す前は、Arch で ffmpeg の試しのエンコードが同梱の古い libstdc++ で失敗し、ハードウェアエンコーダーが「無し」になった。
    - 同じく、PyInstaller の起動部の値（`_PYI_*`）は起動の最初に消し、PySide6 の起動時フックが足す `QT_PLUGIN_PATH`・
      `QML2_IMPORT_PATH` は `QApplication` を作った後に消す（`forget_bundled_qt_paths`。消す前に `QCoreApplication.setLibraryPaths`
      でアプリ自身の探し場所を固定する）。消す前は、「フォルダを開く」で起動した Arch の Dolphin（PC の Qt 6.12）が同梱の
      Qt 6.11 の部品（Wayland の部品など）を読み込んでいた。Windows の .exe も同じ（`_PYI_*`・Qt の値）。
    - メニューの項目は `ts_smartcut.desktop`。アプリが `setDesktopFileName("ts_smartcut")` で名乗る名前と同じにしないと、
      KDE の Wayland でタスクバーのアイコンが汎用のものになる。
  - **Linux の配布物は Ubuntu 24.04（WSL）で作る**（2026-11-15 決定）。PyInstaller の配布物は、作った PC の glibc 以降でしか
    動かない。新しいディストリビューション（Arch など）で作ると、古いディストリビューションでは起動しない。24.04 で作って
    「glibc 2.39 以降」とし（README の「必要なもの」）、最後の動きの確かめは新しい PC（Arch）で行う。
    - 作り方: WSL に Ubuntu 24.04 を入れ（`wsl --install -d Ubuntu-24.04 --location <D ドライブなど> --no-launch`。作る作業は
      root でよい）、apt で build-essential・ffmpeg・libmpv2・Qt が使う X / Wayland の部品・libgtk-3-0t64 を入れ、rustup と
      uv（Python 3.14 を `uv python install 3.14`）を入れて、`uv venv` に `PySide6==6.11.2`・pyinstaller・`./native` を入れて
      `build_release.py`。`install.sh` は root を断るので、確かめは一般の利用者で行う。
    - `.sh`・`.desktop` は `.gitattributes` で常に LF にする。Windows の作業フォルダ（CRLF で取り出す）から写して作った時、
      `install.sh` が `set: Illegal option -` で動かなかった。
    - 確かめたこと（2026-11-15、Ubuntu 24.04 の WSLg）: tar.gz 54.1 MB（165 件、展開 146 MB）、必要な glibc は 2.38 まで、
      `_internal` の部品に足りないものは無い。入れる（「GTK3 found」で GTK3 の見た目の部品が入る）→ 起動して PC の libmpv
      （0.37、API 2.2）で mpv につながる → GTK3 の見た目の部品が PC の GTK3・glib で読み込める → 更新（設定と記録は残り、
      古い版だけの部品は消える）→ 削除（「N」で設定・記録と uninstall.sh が残り、`--purge` で全部消える）。ibus での日本語の
      入力は確かめていない。
