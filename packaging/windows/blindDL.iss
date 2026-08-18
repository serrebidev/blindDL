; Copyright (c) serrebidev and contributors
; This file is part of blindDL.
; SPDX-License-Identifier: MIT

#define MyAppName "blindDL"
#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif
#ifndef MyAppArch
  #define MyAppArch "x64"
#endif
#define MyAppPublisher "serrebidev"
#define MyAppURL "https://github.com/serrebidev/blindDL"
#define MyAppExeName "blindDL.exe"

[Setup]
AppId={{656F03B0-B9A0-5C26-8F6C-68577B4F9D7D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
OutputDir=..\..\release
OutputBaseFilename=blindDL-Setup-v{#MyAppVersion}-windows-{#MyAppArch}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
LicenseFile=..\..\LICENSE
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "torrentassoc"; Description: "Open torrent files and magnet links with blindDL"; GroupDescription: "File types:"

; Setup runs elevated, so HKA is HKLM and these are the machine-wide half of
; the association. blindDL writes the per-user half itself, from Settings or
; the question it asks on first run, because changing your mind afterwards
; should not need an administrator.
;
; Capabilities and RegisteredApplications are what put blindDL in Settings,
; Apps, Default apps. Windows keeps the user's actual choice of default in a
; key only it may write, so this makes blindDL available and pre-selected
; where nothing else has claimed the type -- it cannot overrule a choice the
; user has already made, by design.
[Registry]
Root: HKA; Subkey: "Software\Classes\blindDL.torrent"; ValueType: string; ValueName: ""; ValueData: "BitTorrent file"; Flags: uninsdeletekey; Tasks: torrentassoc
Root: HKA; Subkey: "Software\Classes\blindDL.torrent\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#MyAppExeName},0"; Tasks: torrentassoc
Root: HKA; Subkey: "Software\Classes\blindDL.torrent\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: torrentassoc
Root: HKA; Subkey: "Software\Classes\.torrent\OpenWithProgids"; ValueType: string; ValueName: "blindDL.torrent"; ValueData: ""; Flags: uninsdeletevalue; Tasks: torrentassoc
Root: HKA; Subkey: "Software\Classes\.torrent"; ValueType: string; ValueName: ""; ValueData: "blindDL.torrent"; Flags: uninsdeletevalue; Tasks: torrentassoc

Root: HKA; Subkey: "Software\Classes\blindDL.magnet"; ValueType: string; ValueName: ""; ValueData: "URL:Magnet Link"; Flags: uninsdeletekey; Tasks: torrentassoc
Root: HKA; Subkey: "Software\Classes\blindDL.magnet"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""; Tasks: torrentassoc
Root: HKA; Subkey: "Software\Classes\blindDL.magnet\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#MyAppExeName},0"; Tasks: torrentassoc
Root: HKA; Subkey: "Software\Classes\blindDL.magnet\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: torrentassoc
Root: HKA; Subkey: "Software\Classes\magnet"; ValueType: string; ValueName: ""; ValueData: "URL:Magnet Link"; Flags: uninsdeletekey; Tasks: torrentassoc
Root: HKA; Subkey: "Software\Classes\magnet"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""; Tasks: torrentassoc
Root: HKA; Subkey: "Software\Classes\magnet\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: torrentassoc

Root: HKA; Subkey: "Software\blindDL\Capabilities"; ValueType: string; ValueName: "ApplicationName"; ValueData: "blindDL"; Flags: uninsdeletekey; Tasks: torrentassoc
Root: HKA; Subkey: "Software\blindDL\Capabilities"; ValueType: string; ValueName: "ApplicationDescription"; ValueData: "Accessible media downloader"; Tasks: torrentassoc
Root: HKA; Subkey: "Software\blindDL\Capabilities\FileAssociations"; ValueType: string; ValueName: ".torrent"; ValueData: "blindDL.torrent"; Tasks: torrentassoc
Root: HKA; Subkey: "Software\blindDL\Capabilities\URLAssociations"; ValueType: string; ValueName: "magnet"; ValueData: "blindDL.magnet"; Tasks: torrentassoc
Root: HKA; Subkey: "Software\RegisteredApplications"; ValueType: string; ValueName: "blindDL"; ValueData: "Software\blindDL\Capabilities"; Flags: uninsdeletevalue; Tasks: torrentassoc

[Files]
Source: "..\..\dist\blindDL\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\blindDL"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\blindDL"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch blindDL"; Flags: nowait postinstall skipifsilent
