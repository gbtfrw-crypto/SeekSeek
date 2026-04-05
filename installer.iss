; Inno Setup 스크립트 — SeekSeek 인스톨러
; Inno Setup 6.x 필요: https://jrsoftware.org/isinfo.php

#define MyAppName      "SeekSeek"
#define MyAppVersion   "1.0.0"
#define MyAppPublisher "gbtfrw-crypto"
#define MyAppURL       "https://github.com/gbtfrw-crypto/SeekSeek"
#define MyAppExeName   "SeekSeek.exe"
#define BuildDir       "dist\SeekSeek"

[Setup]
AppId={{B4E2A1C3-7F5D-4A2E-9B8C-3D6F0E1A2B4C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir=dist
OutputBaseFilename=SeekSeek-{#MyAppVersion}-Setup
SetupIconFile=assets\icon.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayIcon={app}\{#MyAppExeName}
MinVersion=10.0

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면 바로가기 만들기"; GroupDescription: "추가 작업:"; Flags: unchecked

[Files]
Source: "{#BuildDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{#MyAppName} 제거"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{#MyAppName} 시작"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallDelete]
; 앱 데이터는 유지 (사용자 설정·인덱스 보존)
