$ErrorActionPreference = 'Stop'
throw '采集暂未启用；本版本禁止启动Worker，已有记录保留。'
$runtimePath = 'E:\douyin-f2-counts\.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $runtimePath)) { throw 'F2运行环境不存在，请检查E盘' }
Start-Process -FilePath $runtimePath -ArgumentList ('"{0}"' -f (Join-Path $PSScriptRoot 'f2_worker.py')) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
