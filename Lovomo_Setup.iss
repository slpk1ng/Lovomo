[Setup]
AppId={{8A31C4E7-52B4-4E19-9C0A-7D6E1F0B93A2}
AppName=Lovomo
AppVersion=v1.2.4.0
AppPublisher=Slpk1ng
DefaultDirName={autopf}\Lovomo
DisableDirPage=no
Uninstallable=yes
; 用户数据（config.json / data 文件夹）固定写在 %LOCALAPPDATA%\Lovomo，不跟安装目录绑在
; 一起 —— 换目录重装、覆盖升级都能接着用。日志优先写安装目录，安装目录不可写时同样退到
; %LOCALAPPDATA%\Lovomo。所以程序装在哪里都不影响数据，也不需要提权。
; lowest 让普通安装落到 %LOCALAPPDATA%\Programs\Lovomo（不弹 UAC）；
; 用管理员运行仍可选择「为所有用户安装」。
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; 将图标路径改为桌面的绝对路径
SetupIconFile=C:\Users\zhanglj\Desktop\Lovomo\icon.ico
OutputDir=output
OutputBaseFilename=Lovomo_Setup
Compression=lzma2
SolidCompression=yes
DisableProgramGroupPage=yes

; ===== 版本号显示 =====
VersionInfoVersion=1.2.4.0
VersionInfoProductVersion=1.2.4.0
VersionInfoProductName=Lovomo
VersionInfoCompany=Slpk1ng

; ===== 安装时显示免责声明 =====
LicenseFile=C:\Users\zhanglj\Desktop\Lovomo\DISCLAIMER.txt

[Files]
; 将源文件路径改为桌面的绝对路径（确保它直接指向 dist\Lovomo 文件夹）
Source: "C:\Users\zhanglj\Desktop\Lovomo\dist\Lovomo\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; AGPL-3.0 第 4 条要求随程序一起提供完整协议文本，因此把 LICENSE 一并安装到程序目录
Source: "C:\Users\zhanglj\Desktop\Lovomo\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "C:\Users\zhanglj\Desktop\Lovomo\DISCLAIMER.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autodesktop}\Lovomo"; Filename: "{app}\Lovomo.exe"; Tasks: desktopicon
Name: "{autoprograms}\Lovomo"; Filename: "{app}\Lovomo.exe"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标："; Flags: unchecked

[Run]
Filename: "{app}\Lovomo.exe"; Description: "立即运行 Lovomo"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载程序只会删除它自己装进去的文件。程序运行时产生的日志、写盘临时文件不在
; 安装清单里，不显式列出就会变成残留。用户数据（config.json / data / plugins）不在这里，
; 它们要么保留，要么由卸载时的询问决定是否删除 —— 绝不能默认删掉。
Type: files; Name: "{app}\*.log"
Type: files; Name: "{app}\config.json.tmp"
Type: files; Name: "{app}\config.json.corrupt"
; 数据现在落在用户目录里，这两条对应它那边的写盘残留
Type: files; Name: "{localappdata}\Lovomo\*.log"
Type: files; Name: "{localappdata}\Lovomo\config.json.tmp"
Type: files; Name: "{localappdata}\Lovomo\config.json.corrupt"
; 在线更新的安装包（下好还没装的话也在这里）—— 卸载时一并清掉
Type: filesandordirs; Name: "{localappdata}\Lovomo\update"

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Code]
const
  AppExeName = 'Lovomo.exe';
  { 与 main.py 的 _INSTANCE_MUTEX_NAME 一致：用它判断程序是否还在运行 }
  AppMutexName = 'Local\Lovomo.SingleInstance';

var
  RemoveSettings: Boolean;
  RemoveUserData: Boolean;
  RemovePluginData: Boolean;
  LeftoverPaths: String;

{ 关闭正在运行的 Lovomo。

  程序把窗口收进托盘而不是退出，所以先发一次普通结束（有机会走正常退出流程，
  窗口几何与运行状态能落盘），几秒后仍在就直接结束进程。安装与卸载都必须先关掉它：
  exe 与 app 目录下的日志/配置文件被占着时，覆盖安装会失败、卸载会只删一半。

  返回 False 表示怎么都关不掉（例如被别的东西护着），调用方据此提示用户。 }
function CloseRunningApp: Boolean;
var
  ResultCode, I: Integer;
begin
  Result := True;
  { 先无条件请求一次结束：太老的版本没有下面这个互斥体，光靠它判断不出在不在跑。
    窗口会收进托盘而不是退出，所以这一步基本等不到结果，很快就转成强制结束 }
  Exec('taskkill.exe', '/IM ' + AppExeName, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if not CheckForMutexes(AppMutexName) then
  begin
    Sleep(500);   { 没有判断依据，只等一下让系统放开文件句柄 }
    Exit;
  end;
  for I := 1 to 4 do
  begin
    if not CheckForMutexes(AppMutexName) then
      Exit;
    Sleep(300);
  end;
  Exec('taskkill.exe', '/F /IM ' + AppExeName, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  for I := 1 to 10 do
  begin
    if not CheckForMutexes(AppMutexName) then
      Exit;
    Sleep(300);
  end;
  Result := False;
end;

{ 安装前先关掉旧实例：覆盖安装（含程序内「在线更新」）走的就是这条路径。 }
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not CheckForMutexes(AppMutexName) then
  begin
    CloseRunningApp;   { 认不出实例时也请求一次结束，避免老版本占着 exe }
    Exit;
  end;
  if not WizardSilent() then
  begin
    WizardForm.StatusLabel.Caption := '正在关闭正在运行的 Lovomo …';
    WizardForm.Refresh;
  end;
  if not CloseRunningApp then
    Result := '无法关闭正在运行的 Lovomo，请手动退出程序后重新安装。';
  if not WizardSilent() then
    WizardForm.StatusLabel.Caption := '';
end;

function TryDeleteFile(const FilePath: String): Boolean;
var
  I: Integer;
begin
  Result := True;
  if not FileExists(FilePath) then
    Exit;
  Result := False;
  { 日志/配置可能被还在运行的程序占着，等几轮再决定失败 }
  for I := 1 to 5 do
  begin
    if DeleteFile(FilePath) then
    begin
      Result := True;
      Exit;
    end;
    Sleep(400);
  end;
end;

function TryDeleteTree(const DirPath: String): Boolean;
var
  I: Integer;
begin
  Result := True;
  if not DirExists(DirPath) then
    Exit;
  Result := False;
  for I := 1 to 5 do
  begin
    if DelTree(DirPath, True, True, True) then
    begin
      Result := True;
      Exit;
    end;
    Sleep(400);
  end;
end;

{ 用户数据有两处落点：新版本固定写在用户目录（%LOCALAPPDATA%\Lovomo），老版本写在程序
  目录里。卸载时两处都要清，只盯程序目录会漏掉真正在用的那份。 }
procedure PurgeEntry(const BaseDir, Name: String; const IsDir: Boolean);
var
  Target: String;
begin
  Target := BaseDir + '\' + Name;
  if IsDir then
  begin
    if DirExists(Target) and (not TryDeleteTree(Target)) then
      LeftoverPaths := LeftoverPaths + #13#10 + Target;
  end
  else if FileExists(Target) and (not TryDeleteFile(Target)) then
    LeftoverPaths := LeftoverPaths + #13#10 + Target;
end;

procedure PurgeBoth(const Name: String; const IsDir: Boolean);
begin
  PurgeEntry(ExpandConstant('{app}'), Name, IsDir);
  PurgeEntry(ExpandConstant('{localappdata}\Lovomo'), Name, IsDir);
end;

procedure AskRemoveUserData;
begin
  RemoveSettings := False;
  RemoveUserData := False;
  RemovePluginData := False;

  RemoveSettings := MsgBox(
    '是否一并删除配置文件 config.json？' + #13#10 + #13#10 +
    '里面是模型、TTS、NapCat 连接等全部设置（密钥为加密存储）。' + #13#10 +
    '删除后无法找回；选「否」则保留，下次重装可直接沿用。',
    mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;

  RemoveUserData := MsgBox(
    '是否一并删除聊天记录与用户数据（data 文件夹）？' + #13#10 + #13#10 +
    '里面是会话记忆、表情包、待办提醒、统计数据库、用户画像等。' + #13#10 +
    '删除后无法找回；选「否」则原样保留。',
    mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;

  RemovePluginData := MsgBox(
    '是否一并删除已安装的插件与本地记录？' + #13#10 + #13#10 +
    '包括插件本身、插件自己的设置与数据、发布记录、窗口位置。' + #13#10 +
    '删除后无法找回；选「否」则原样保留，重装后还能接着用。',
    mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    RemoveSettings := False;
    RemoveUserData := False;
    RemovePluginData := False;
    LeftoverPaths := '';
    { 用户确认卸载之后、真正删文件之前：先把还在运行的程序关掉，
      否则 exe 与日志/配置文件被占用，删除会失败或只删掉一半 }
    if CheckForMutexes(AppMutexName) then
    begin
      if (not UninstallSilent()) and (UninstallProgressForm <> nil) then
      begin
        UninstallProgressForm.StatusLabel.Caption := '正在关闭正在运行的 Lovomo …';
        UninstallProgressForm.Refresh;
      end;
      if (not CloseRunningApp) and (not UninstallSilent()) then
        MsgBox('未能关闭正在运行的 Lovomo，本次删除可能不完整，'
               + '请手动退出程序后再卸载一次。', mbError, MB_OK);
    end
    else
      CloseRunningApp;   { 认不出实例时也请求一次结束，避免老版本占着文件 }
    { 静默卸载（覆盖安装升级时由安装程序自动触发）没有对话可问，一律保留用户数据 }
    if not UninstallSilent() then
    begin
      AskRemoveUserData;
      if RemoveSettings then
        PurgeBoth('config.json', False);
      if RemoveUserData then
        PurgeBoth('data', True);
      if RemovePluginData then
      begin
        { 插件、插件数据、发布记录、窗口位置恒定在用户目录里，只清这一处 }
        PurgeEntry(ExpandConstant('{localappdata}\Lovomo'), 'plugins', True);
        PurgeEntry(ExpandConstant('{localappdata}\Lovomo'), 'publish_state.json', False);
        PurgeEntry(ExpandConstant('{localappdata}\Lovomo'), 'window_geometry.json', False);
        PurgeEntry(ExpandConstant('{localappdata}\Lovomo'), 'webview', True);
      end;
    end;
  end
  else if CurUninstallStep = usPostUninstall then
  begin
    if LeftoverPaths <> '' then
      MsgBox('以下内容被其他程序占用，未能自动删除，请关闭后手动删除：' + LeftoverPaths,
             mbInformation, MB_OK);
  end;
end;
