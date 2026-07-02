; Inno Setup script for Developer Activity desktop agent
; Build: compile this file with Inno Setup 6+
; Prerequisite: dist\DevActivityAgent.exe must exist (run pyinstaller first)

#define AppName     "Developer Activity"
#define AppVersion  "1.0.0"
#define AppExe      "DevActivityAgent.exe"
#define AppPublisher "Your Company"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL=https://your-app.com
DefaultDirName={localappdata}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=DevActivitySetup
Compression=lzma2/ultra64
SolidCompression=yes
PrivilegesRequired=lowest
; Require Windows 10+ (WebView2 is available from Win10 1803+)
MinVersion=10.0.17134
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; Main executable
Source: "dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; Start menu shortcut — normal launch (shows window)
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
; Desktop shortcut (optional — remove if not wanted)
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Registry]
; Add to Windows startup — runs silently in background at boot
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
  ValueType: string; ValueName: "{#AppName}"; \
  ValueData: """{app}\{#AppExe}"" --startup"; \
  Flags: uninsdeletevalue

[Run]
; Launch the app after install (shows window for first login)
Filename: "{app}\{#AppExe}"; \
  Description: "Launch {#AppName}"; \
  Flags: nowait postinstall skipifsilent

[UninstallRun]
; Remove from Windows startup and kill any running instance before uninstall
Filename: "taskkill"; Parameters: "/f /im {#AppExe}"; Flags: runhidden; RunOnceId: "KillAgent"

[UninstallDelete]
; Clean up any leftover files in the install dir
Type: filesandordirs; Name: "{app}"
