; TS_SmartCut Windows installer (Inno Setup 6)
;
; build_release.py passes /DAppVersion=... /DSourceDir=<dist\TS_SmartCut> to ISCC.
; - Installs per user into %LOCALAPPDATA%\Programs\TS_SmartCut (no administrator rights). The app keeps its
;   settings, history DB and logs next to TS_SmartCut.exe, so the folder must be writable (Program Files is not).
; - An update replaces the program files only; settings.json, cut_history.db, logs\, Logo\ and so on stay.
; - Uninstalling asks whether to delete those too (default: keep them).
; - ffmpeg, ffprobe and mpv (libmpv-2.dll) are not bundled; the last page says which ones were not found on PATH.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\TS_SmartCut"
#endif

[Setup]
AppId={{6F0D2B57-4C1E-4E61-9C3A-2B7D5A1E8C44}
AppName=TS_SmartCut
AppVersion={#AppVersion}
AppVerName=TS_SmartCut {#AppVersion}
AppPublisher=TS_SmartCut
AppPublisherURL=https://github.com/babaken/TS_SmartCut
DefaultDirName={localappdata}\Programs\TS_SmartCut
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputBaseFilename=TS_SmartCut-{#AppVersion}-setup
SetupIconFile=..\..\ts_smartcut\gui\resources\icon.ico
UninstallDisplayIcon={app}\TS_SmartCut.exe
UninstallDisplayName=TS_SmartCut
LicenseFile=..\..\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "ja"; MessagesFile: "compiler:Languages\Japanese.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
ja.DesktopIcon=デスクトップにショートカットを作る
en.DesktopIcon=Create a desktop shortcut
ja.LaunchApp=TS_SmartCut を起動する
en.LaunchApp=Launch TS_SmartCut
ja.MissingTools=次のものが PATH に見つかりませんでした:%n%n%1%nTS_SmartCut は ffmpeg・ffprobe（変換と読み込み）と mpv の libmpv-2.dll（プレビュー）を使います。入れてから TS_SmartCut を起動してください（README の「必要なもの」）。
en.MissingTools=The following were not found on PATH:%n%n%1%nTS_SmartCut uses ffmpeg and ffprobe (cutting and reading) and mpv's libmpv-2.dll (the preview). Install them before starting TS_SmartCut (see "Requirements" in the README).
ja.DeleteData=設定・カットの記録（cut_history.db）・ログ・キャッシュ・Logo フォルダ（取り込んだ .lgd）も削除しますか？%n%n「いいえ」を選ぶと残します（入れ直した時にそのまま使えます）。
en.DeleteData=Also delete the settings, the cut history (cut_history.db), the logs, the caches and the Logo folder (imported .lgd files)?%n%nChoose "No" to keep them (a reinstall uses them again).

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIcon}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[InstallDelete]
; Files of an older version that the new one no longer has must not stay in _internal
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\TS_SmartCut"; Filename: "{app}\TS_SmartCut.exe"
Name: "{autodesktop}\TS_SmartCut"; Filename: "{app}\TS_SmartCut.exe"; Tasks: desktopicon

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Run]
Filename: "{app}\TS_SmartCut.exe"; Description: "{cm:LaunchApp}"; Flags: postinstall nowait skipifsilent

[Code]
{ True when Name is found in a folder on PATH }
function OnPath(const Name: String): Boolean;
begin
  Result := FileSearch(Name, GetEnv('PATH')) <> '';
end;

function MissingTools(): String;
begin
  Result := '';
  if not OnPath('ffmpeg.exe') then
    Result := Result + '  ffmpeg.exe' + #13#10;
  if not OnPath('ffprobe.exe') then
    Result := Result + '  ffprobe.exe' + #13#10;
  if not (OnPath('libmpv-2.dll') or OnPath('mpv-2.dll') or OnPath('mpv-1.dll')) then
    Result := Result + '  libmpv-2.dll (mpv)' + #13#10;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Missing: String;
begin
  if (CurStep = ssPostInstall) and not WizardSilent() then
  begin
    Missing := MissingTools();
    if Missing <> '' then
      MsgBox(FmtMessage(CustomMessage('MissingTools'), [Missing]), mbInformation, MB_OK);
  end;
end;

{ What the app writes next to TS_SmartCut.exe (system/app_dir.py, temp_file_manager.py, lgd_import.py) }
procedure DeleteAppData(const Dir: String);
begin
  DeleteFile(Dir + '\settings.json');
  DeleteFile(Dir + '\recent_files.json');
  DeleteFile(Dir + '\hardware_encoders.json');
  DeleteFile(Dir + '\shortcut_presets.json');
  DeleteFile(Dir + '\cut_history.db');
  DeleteFile(Dir + '\cut_history.db-wal');
  DeleteFile(Dir + '\cut_history.db-shm');
  DelTree(Dir + '\logs', True, True, True);
  DelTree(Dir + '\Temp', True, True, True);
  DelTree(Dir + '\KeyframeCache', True, True, True);
  DelTree(Dir + '\Logo', True, True, True);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Dir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    Dir := ExpandConstant('{app}');
    if (not UninstallSilent()) and
       (MsgBox(CustomMessage('DeleteData'), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES) then
      DeleteAppData(Dir);
    RemoveDir(Dir);
  end;
end;
