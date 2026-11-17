; Call Companion - Inno Setup script (one-click, per-user, no admin rights).
; Built by build.ps1, which passes the version from version.py:
;   ISCC.exe /DAppVersion=0.5.0-beta /DAppNumVersion=0.5.0.0 installer.iss
; Input:  dist\CallCompanion\  (PyInstaller one-folder build + config.json + extension\)
; Output: dist\installer\CallCompanion-Setup-<version>.exe
;
; What it does
;   - installs to %LOCALAPPDATA%\Programs\CallCompanion (no UAC prompt; same folder the old install.ps1 used)
;   - Start menu entries + autostart shortcut in %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
;   - removes the old HKCU "Run" autostart entry (source / install.ps1 era) so the app never starts twice
;   - registers in Settings > Apps (Add/Remove Programs) with an uninstaller
;   - closes a running Call Companion before copying, starts it again at the end (also in /VERYSILENT mode,
;     which is how the auto-updater runs it)
; Requires Inno Setup 6.3 or newer (x64compatible).

#ifndef AppVersion
  #error AppVersion is not defined - run build.ps1, or ISCC /DAppVersion=x.y.z /DAppNumVersion=x.y.z.0 installer.iss
#endif
#ifndef AppNumVersion
  #define AppNumVersion "0.0.0.0"
#endif

#define AppName   "Call Companion"
#define AppExe    "CallCompanion.exe"
#define AppRepo   "https://github.com/dekanyrichard87-bit/call-companion"
; 1.0.0: same as version.EXTENSION_STORE_URL (the Chrome extension's Web Store page)
#define ExtStoreUrl "https://chromewebstore.google.com/detail/hmmimbnglnhilhbakdboepenlfkjgjpj"
#define HasHungarian FileExists(AddBackslash(CompilerPath) + "Languages\Hungarian.isl")

[Setup]
; Never change AppId: it links updates and the uninstaller to the existing installation.
AppId={{B0F10CFE-EF77-402A-A104-083DF1402C95}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Dékány Richárd
AppCopyright=© 2026 Dékány Richárd
AppPublisherURL={#AppRepo}
AppSupportURL={#AppRepo}/issues
AppUpdatesURL={#AppRepo}/releases
VersionInfoVersion={#AppNumVersion}
VersionInfoProductTextVersion={#AppVersion}
VersionInfoDescription={#AppName} Setup

PrivilegesRequired=lowest
DefaultDirName={autopf}\CallCompanion
UsePreviousAppDir=yes
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
DisableWelcomePage=yes
ShowLanguageDialog=no
LanguageDetectionMethod=uilanguage

ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

OutputDir=dist\installer
OutputBaseFilename=CallCompanion-Setup-{#AppVersion}
SetupIconFile=assets\app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
SetupLogging=yes
; The [Code] section closes the app itself; the Restart Manager prompt is not needed.
CloseApplications=no
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
#if HasHungarian
Name: "hungarian"; MessagesFile: "compiler:Languages\Hungarian.isl"
#endif

[CustomMessages]
english.ExtHint=Chrome extension (one time, if Chrome does not have it yet):%nopen its Chrome Web Store page and click "Add to Chrome":%n%1
english.ExtRemove=Call Companion was removed.%n%nPlease also remove the "Call Companion" extension in Chrome (chrome://extensions).
#if HasHungarian
hungarian.ExtHint=Chrome bővítmény (egyszer kell, ha a Chrome-ban még nincs meg):%nnyissa meg a Chrome Webáruházban, és kattintson a „Hozzáadás a Chrome-hoz” gombra:%n%1
hungarian.ExtRemove=A Call Companion eltávolítva.%n%nA Chrome-ból is távolítsa el a "Call Companion" bővítményt (chrome://extensions).
#endif

[InstallDelete]
; A fresh runtime folder on every update: no stale DLLs / .pyd files from an older build.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\extension"
; 1.0.0: the extension comes from the Chrome Web Store; remove the old "extension folder" Start menu shortcut
Type: files; Name: "{userprograms}\{#AppName}\Chrome extension folder.lnk"
Type: files; Name: "{userprograms}\{#AppName}\Chrome bővítmény mappája.lnk"

[Files]
; config.json next to the exe is the shipped config and is replaced on every update.
; Personal overrides go to %LOCALAPPDATA%\CallCompanion\config.json (merged on top, never touched).
Source: "dist\CallCompanion\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\{#AppName}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--show"; WorkingDir: "{app}"
; Autostart at logon (per user): %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Call Companion.lnk
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"

[Registry]
; Old autostart method (python main.py --install-autostart / install.ps1): remove it, the Startup shortcut replaces it.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "CallCompanion"; Flags: deletevalue uninsdeletevalue

[Run]
; Without "skipifsilent" this also runs after a /VERYSILENT update -> the app restarts itself.
Filename: "{app}\{#AppExe}"; Parameters: "--after-install"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall

[UninstallDelete]
; Downloaded updates. Logs and personal config in %LOCALAPPDATA%\CallCompanion are kept.
Type: filesandordirs; Name: "{localappdata}\CallCompanion\updates"

[Code]
procedure StopApp();
var
  ResultCode: Integer;
begin
  { Closes only this user's instances (no admin rights). Exit code 128 = not running: fine. }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(700);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopApp();
  Result := '';
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpFinished then
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 +
      FmtMessage(CustomMessage('ExtHint'), ['{#ExtStoreUrl}']);
end;

function InitializeUninstall(): Boolean;
begin
  StopApp();
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and (not UninstallSilent) then
    MsgBox(CustomMessage('ExtRemove'), mbInformation, MB_OK);
end;
