$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$runtimeSource = 'E:\道路救援看板\road-rescue-dashboard'
$desktopRoot = Join-Path $projectRoot 'desktop'
$resourcesRoot = Join-Path $desktopRoot 'resources'
New-Item -ItemType Directory -Path $desktopRoot,$resourcesRoot,(Join-Path $resourcesRoot 'app'),(Join-Path $resourcesRoot 'python'),(Join-Path $resourcesRoot 'backend\data') -Force | Out-Null
Get-ChildItem -LiteralPath $runtimeSource -File | Where-Object { $_.Extension -ne '.exe' -and $_.Name -ne 'uninstallerIcon.ico' } | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $desktopRoot -Force }
Copy-Item -LiteralPath (Join-Path $runtimeSource '道路救援图片看板.exe') -Destination (Join-Path $desktopRoot '抖音账号工作台.exe') -Force
Copy-Item -LiteralPath (Join-Path $runtimeSource 'locales') -Destination $desktopRoot -Recurse -Force
Get-ChildItem -LiteralPath (Join-Path $runtimeSource 'resources\python') -File | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $resourcesRoot 'python') -Force }
Set-Content -LiteralPath (Join-Path $resourcesRoot 'python\python311._pth') -Encoding ascii -Value "python311.zip`n.`n"
Copy-Item -Path (Join-Path $projectRoot 'desktop-src\*') -Destination (Join-Path $resourcesRoot 'app') -Force
foreach ($name in @('app.py','desktop_server.py','web_server.py','backup.py','collection.py','account_lifecycle.py','phone_cards.py','phone_devices.py','auth.py','publishing_layout.py','operations.py','account_service.py','data_exchange.py','backup_management.py','workspace_service.py','nurture.py','batch_edit.py')) { Copy-Item -LiteralPath (Join-Path $projectRoot $name) -Destination (Join-Path $resourcesRoot 'backend') -Force }
Copy-Item -LiteralPath (Join-Path $projectRoot 'static') -Destination (Join-Path $resourcesRoot 'backend') -Recurse -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'data\seed.json') -Destination (Join-Path $resourcesRoot 'backend\data\seed.json') -Force
Write-Output "Desktop executable: $desktopRoot\抖音账号工作台.exe"
