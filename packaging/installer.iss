; Pred Overlay setup: the exe folder from PyInstaller (dist\PredOverlay), per user, no
; administrator rights. Built by CI after the self-test:
;   ISCC.exe /DAppVersion=0.5.25 packaging\installer.iss   ->  dist\PredOverlay-v0.5.25-setup.exe
;
; The program folder is fixed at %LOCALAPPDATA%\Programs\PredOverlay, the same place the
; zip goes, because the app updates itself by swapping that folder (updater.py), and the
; updater only swaps a folder that holds nothing but the app. So the uninstaller lives
; beside it, in PredOverlay.uninstall, and the uninstall deletes the whole program folder
; rather than the files this setup copied: an update may have replaced them all.
; Shortcuts and autostart are made by the app on its first start, as for the zip
; (--uninstall resets that, so a reinstall that kept the settings makes them again).
; A silent install (/VERYSILENT) starts nothing: the first start does that setup.
; Known gap: a setup run in the seconds while the app's own update swaps the folder
; can collide with the swap script.

#ifndef AppVersion
  #error Pass the version: ISCC.exe /DAppVersion=x.y.z packaging\installer.iss
#endif

[Setup]
; Keep the AppId: Windows' Apps list and later setups find an install by it, and
; predoverlay/install.py (SETUP_KEY) keeps that entry's version current after an update.
AppId={{2104A709-9A62-4DDB-B3CF-AF9AF36B84E8}
AppName=Pred Overlay
AppVersion={#AppVersion}
AppVerName=Pred Overlay {#AppVersion}
UninstallDisplayName=Pred Overlay
AppPublisher=TheWeaklessOne
AppPublisherURL=https://github.com/TheWeaklessOne/pred-overlay
AppSupportURL=https://github.com/TheWeaklessOne/pred-overlay/issues
AppUpdatesURL=https://github.com/TheWeaklessOne/pred-overlay/releases
VersionInfoVersion={#AppVersion}
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\PredOverlay
DisableDirPage=yes
DisableProgramGroupPage=yes
UninstallFilesDir={localappdata}\Programs\PredOverlay.uninstall
UninstallDisplayIcon={app}\PredOverlay.exe
SetupIconFile=..\predoverlay\assets\icon.ico
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; The running app is ended by its own --quit below, not by the Restart Manager.
CloseApplications=no
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
OutputDir=..\dist
OutputBaseFilename=PredOverlay-v{#AppVersion}-setup

[InstallDelete]
; The files of the build this one replaces must not stay next to the new ones.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\PredOverlay\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Run]
Filename: "{app}\PredOverlay.exe"; Description: "Start Pred Overlay"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; PredOverlay.exe is the windowed exe: these commands open no console window.
Filename: "{app}\PredOverlay.exe"; Parameters: "--quit"; Flags: runhidden waituntilterminated; RunOnceId: "Quit"
Filename: "{app}\PredOverlay.exe"; Parameters: "--uninstall"; Flags: runhidden waituntilterminated; RunOnceId: "Integration"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
Type: filesandordirs; Name: "{app}.update"
Type: filesandordirs; Name: "{app}.old"
Type: filesandordirs; Name: "{app}.failed"

[Code]
function AppExe(): String;
begin
  Result := ExpandConstant('{app}\PredOverlay.exe');
end;

{ True when the folder holds nothing but the app's own files (or does not exist):
  the uninstall deletes the whole folder, so it must never hold anything else. }
function OnlyAppFiles(Folder: String): Boolean;
var
  Found: TFindRec;
  Name: String;
begin
  Result := True;
  if FindFirst(Folder + '\*', Found) then
  try
    repeat
      Name := Found.Name;
      if (Name <> '.') and (Name <> '..') and (CompareText(Name, 'PredOverlay.exe') <> 0)
         and (CompareText(Name, 'PredOverlayCLI.exe') <> 0) and (CompareText(Name, '_internal') <> 0) then
        Result := False;
    until not Result or not FindNext(Found);
  finally
    FindClose(Found);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  Result := '';
  { /DIR= still works with the folder page hidden; the app updates itself only here. }
  if CompareText(ExpandConstant('{app}'), ExpandConstant('{localappdata}\Programs\PredOverlay')) <> 0 then
  begin
    Result := 'Pred Overlay installs only to ' + ExpandConstant('{localappdata}\Programs\PredOverlay') + '.';
    exit;
  end;
  if not OnlyAppFiles(ExpandConstant('{app}')) then
  begin
    Result := ExpandConstant('{app}') + ' holds other files too. Move them out, then run this setup again.';
    exit;
  end;
  { An earlier copy is asked to quit, as from its tray menu. Exit code 1 is the only
    "still running" (app.quit_running); anything else (a damaged build being repaired)
    goes on, and a file still in use stops the copy with Inno Setup's own message. }
  if FileExists(AppExe()) then
    if Exec(AppExe(), '--quit', '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 1) then
      Result := 'Pred Overlay is still running. Quit it from its tray menu, then run this setup again.';
end;

{ Settings, the sign-in and the logs live in %LOCALAPPDATA%\PredOverlay: ask before deleting. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
begin
  Data := ExpandConstant('{localappdata}\PredOverlay');
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent() and DirExists(Data) then
    if MsgBox('Also delete your Pred Overlay settings, sign-in and logs?' + #13#10#13#10 + Data,
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(Data, True, True, True);
end;
