$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonPath = Join-Path $projectRoot 'desktop\resources\python\python.exe'
$taskName = 'DouyinAccountBoardWeb'
$configPath = Join-Path $projectRoot 'data\web-service.json'
$port = 8776
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw '请以管理员身份运行部署脚本' }
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { throw '网站任务已经存在，请使用任务计划程序管理，避免重复部署' }
if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) { throw '8776端口已占用，停止部署' }
if (Test-Path -LiteralPath $configPath) { throw '网站配置已存在，停止部署以免覆盖' }
& $pythonPath -X utf8 -c "import sys,sqlite3,json,secrets,datetime;from pathlib import Path;root=Path(sys.argv[1]);sys.path.insert(0,str(root));import backup;db=root/'data/board.db';stamp=datetime.datetime.now().strftime('%Y%m%d-%H%M%S');target=root/'backups'/('before-web-deployment-'+stamp+'.db');source=sqlite3.connect(db);assert source.execute('select count(*) from auth_users where role=?',('owner',)).fetchone()[0]==1;destination=sqlite3.connect(target);source.backup(destination);destination.close();source.close();config=dict(enabled=True,host='0.0.0.0',port=8776,data_dir=str(root/'data'),worker_token=secrets.token_urlsafe(48),public_origin='');(root/'data/web-service.json').write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8');print('Backup: '+str(target))" $projectRoot
if ($LASTEXITCODE -ne 0) { throw '备份或配置创建失败' }
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls $configPath /inheritance:r /grant:r '*S-1-5-18:(F)' '*S-1-5-32-544:(F)' "*$($currentSid):(F)" | Out-Null
if ($LASTEXITCODE -ne 0) { throw '配置文件权限保护失败' }
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument ('-u "{0}" --config "{1}"' -f (Join-Path $projectRoot 'web_server.py'),$configPath) -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description '抖音账号工作台：统一数据库、登录权限、自动备份；开机后台运行' | Out-Null
New-NetFirewallRule -Name 'DouyinAccountBoardWeb-LAN' -DisplayName '抖音账号工作台网站（本办公室）' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $port -RemoteAddress '192.168.9.0/24' -InterfaceAlias '以太网' -Profile Any | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Output 'Website task installed and started: DouyinAccountBoardWeb'

