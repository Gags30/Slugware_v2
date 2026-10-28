; Inno Setup script for Slugware
; Build the exe first:  python -m PyInstaller --noconfirm --clean --onefile --windowed --name Slugware --icon "Slug.ico" Slugware.py
; Then compile this script:  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" Slugware.iss

#define MyAppName "Slugware"
#define MyAppVersion "2.0"
#define MyAppPublisher "Gags30"
#define MyAppExeName "Slugware.exe"

[Setup]
; A unique AppId keeps upgrades/uninstall tied to the same product across versions.
AppId={{8160E9FD-79E2-4E55-9677-3D39BF4AF890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=installer
OutputBaseFilename=Slugware-Setup-{#MyAppVersion}
SetupIconFile=Slug.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Install per-machine if elevated, otherwise let the user install just for themselves.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\Slugware.exe"; DestDir: "{app}"; Flags: ignoreversion
; Slug.png is loaded at runtime from next to the exe (CODE_DIR), so ship it alongside.
Source: "Slug.png"; DestDir: "{app}"; Flags: ignoreversion
Source: "Slug.ico"; DestDir: "{app}"; Flags: ignoreversion
; The Inter fonts (and their licence) are also loaded from next to the exe.
Source: "fonts\*"; DestDir: "{app}\fonts"; Flags: ignoreversion
; The GPL asks for a copy of the licence to travel with the program.
Source: "LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
