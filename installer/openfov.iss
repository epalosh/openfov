; OpenFOV — Inno Setup installer script.
;
; Builds a single-file installer that drops the Nuitka standalone bundle
; into Program Files, registers a start-menu shortcut, and offers
; optional desktop / start-with-Windows hooks.
;
; Requirements:
;   - Run `pwsh npclient-vendor/build.ps1` first (produces NPClient64.dll,
;     TrackIR.exe under resources/bin/).
;   - Run `pwsh build/nuitka_build.ps1` next (produces dist/openfov.dist/).
;   - Then compile this script with Inno Setup 6:
;       iscc installer/openfov.iss
;     The output `Output/OpenFOV-x.y.z-setup.exe` is what users download.
;
; Conventions:
;   - Per-machine install (Program Files) - standard for TrackIR-class apps.
;   - User data lives under %APPDATA%\OpenFOV\ - left intact on uninstall
;     unless the user opts in to "Remove personal settings."
;   - The NPClient registry pointer is written by the *app* on first run
;     (per-user, no UAC needed), not by the installer.

#define MyAppName "OpenFOV"
#define MyAppPublisher "OpenFOV Project"
#define MyAppURL "https://github.com/epalosh/openfov"
#define MyAppExeName "OpenFOV.exe"
#ifndef MyAppVersion
  ; Keep in sync with pyproject.toml [project.version]. CI can override
  ; with `iscc /DMyAppVersion=1.2.3 installer/openfov.iss`.
  #define MyAppVersion "0.1.0"
#endif

[Setup]
AppId={{F0A91E2F-3D6A-4F9A-BF6F-7E0F2A41B9C4}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf64}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog commandline
OutputBaseFilename=OpenFOV-{#MyAppVersion}-win-x64
SetupIconFile=..\resources\icons\openfov.ico
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
MinVersion=10.0.17763
ChangesAssociations=no
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startupicon"; Description: "Start {#MyAppName} when I log in to Windows"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The Nuitka standalone bundle — everything under dist/openfov.dist/.
; Bundled resources (icon, model, NPClient binaries) come along via the
; --include-data-dir flag in nuitka_build.ps1.
Source: "..\dist\openfov.dist\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; Microsoft Visual C++ 2015-2022 Redistributable (x64).
; OpenFOV's Nuitka bundle uses MSVC runtime DLLs (msvcp140, vcruntime140,
; concrt140). On any modern Windows machine with VS, Office, or a recent
; game installed they're already present — but we ship the redist
; installer and run it silently as a prerequisite on machines that don't
; have it, so the install experience is one click for everyone. The
; binary itself isn't committed to git (~25 MB); build_installer.ps1
; downloads it from Microsoft if missing.
Source: "redist\vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\resources\icons\openfov.ico"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\resources\icons\openfov.ico"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startupicon

[Run]
; Run the VC++ Redistributable installer first, but only if the runtime
; isn't already present (see VCRedistNeedsInstall in [Code]). Silent +
; norestart so the user sees nothing extra; norestart means Windows
; won't reboot mid-install. Exit codes 0 (success), 1638 (already
; installed but caught after our check), and 3010 (install OK but
; reboot required) are all acceptable.
Filename: "{tmp}\vc_redist.x64.exe"; \
    Parameters: "/install /quiet /norestart"; \
    StatusMsg: "Installing Microsoft Visual C++ Runtime..."; \
    Check: VCRedistNeedsInstall

Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Leave %APPDATA%\OpenFOV alone by default — users may want to reinstall
; without losing their profiles. They can manually wipe that folder if
; they want a clean slate.
Type: filesandordirs; Name: "{app}"

[Code]
function InitializeSetup(): Boolean;
begin
  // No setup-time blockers — VC++ Runtime is auto-installed during
  // [Run] (see VCRedistNeedsInstall). Future prereqs (DirectX, etc.)
  // would hook in here.
  Result := True;
end;

function VCRedistNeedsInstall(): Boolean;
var
  Installed: Cardinal;
begin
  // Microsoft writes 'Installed' = 1 to this key when *any* version of
  // the VC++ 2015-2022 x64 runtime is present. The "14.0" path covers
  // VS 2015, 2017, 2019, AND 2022 — Microsoft kept the same key after
  // renaming the runtime, on purpose, so installers like ours don't
  // have to track each version separately.
  if RegQueryDWordValue(HKEY_LOCAL_MACHINE,
       'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
       'Installed', Installed) then
  begin
    Result := Installed <> 1;
    Exit;
  end;
  // No key at all — runtime is missing.
  Result := True;
end;
