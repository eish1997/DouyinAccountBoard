$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$pythonPath = Join-Path $PSScriptRoot 'desktop\resources\python\python.exe'
$resetPath = Join-Path $PSScriptRoot 'reset_owner.py'
$infoText = & $pythonPath -X utf8 $resetPath --info
if ($LASTEXITCODE -ne 0) { [Windows.Forms.MessageBox]::Show([string]$infoText,'无法重置'); exit 1 }
$ownerInfo = $infoText | ConvertFrom-Json
$form = New-Object Windows.Forms.Form
$form.Text = '重置工作台总管理员密码'
$form.ClientSize = New-Object Drawing.Size(450,275)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.TopMost = $true
$label = New-Object Windows.Forms.Label
$label.Text = "管理员登录名：$($ownerInfo.username)`r`n请输入新密码（10至128位）。账号和业务数据保留。"
$label.SetBounds(20,18,410,48)
$form.Controls.Add($label)
$labelOne = New-Object Windows.Forms.Label
$labelOne.Text = '新密码'
$labelOne.SetBounds(20,83,90,25)
$form.Controls.Add($labelOne)
$first = New-Object Windows.Forms.TextBox
$first.UseSystemPasswordChar = $true
$first.MaxLength = 128
$first.SetBounds(115,80,305,27)
$form.Controls.Add($first)
$labelTwo = New-Object Windows.Forms.Label
$labelTwo.Text = '确认新密码'
$labelTwo.SetBounds(20,128,90,25)
$form.Controls.Add($labelTwo)
$second = New-Object Windows.Forms.TextBox
$second.UseSystemPasswordChar = $true
$second.MaxLength = 128
$second.SetBounds(115,125,305,27)
$form.Controls.Add($second)
$errorLabel = New-Object Windows.Forms.Label
$errorLabel.ForeColor = [Drawing.Color]::Firebrick
$errorLabel.SetBounds(20,165,410,40)
$form.Controls.Add($errorLabel)
$button = New-Object Windows.Forms.Button
$button.Text = '确认重置'
$button.SetBounds(285,220,135,32)
$form.Controls.Add($button)
$form.AcceptButton = $button
$button.Add_Click({
    if ($first.Text.Length -lt 10) { $errorLabel.Text = '密码至少10位'; return }
    if ($first.Text -cne $second.Text) { $errorLabel.Text = '两次密码不一致'; return }
    $button.Enabled = $false
    $worker = $null
    try {
        $start = New-Object Diagnostics.ProcessStartInfo
        $start.FileName = $pythonPath
        $start.Arguments = '-X utf8 "' + $resetPath + '" --apply'
        $start.UseShellExecute = $false
        $start.CreateNoWindow = $true
        $start.RedirectStandardInput = $true
        $start.RedirectStandardOutput = $true
        $start.RedirectStandardError = $true
        $start.StandardInputEncoding = New-Object Text.UTF8Encoding($false)
        $start.StandardOutputEncoding = New-Object Text.UTF8Encoding($false)
        $worker = New-Object Diagnostics.Process
        $worker.StartInfo = $start
        [void]$worker.Start()
        $worker.StandardInput.WriteLine((@{password=$first.Text} | ConvertTo-Json -Compress))
        $worker.StandardInput.Close()
        $resultText = $worker.StandardOutput.ReadToEnd()
        $worker.WaitForExit()
        $result = $resultText | ConvertFrom-Json
        if ($worker.ExitCode -ne 0) { throw $result.error }
        $first.Clear(); $second.Clear()
        [Windows.Forms.MessageBox]::Show("重置成功。登录名：$($result.username)`r`n请使用刚设置的新密码登录工作台。",'完成')
        $form.Close()
    } catch { $errorLabel.Text = '重置失败：' + $_.Exception.Message }
    finally { if ($worker) { $worker.Dispose() }; $button.Enabled = $true }
})
[void]$form.ShowDialog()
$first.Clear(); $second.Clear()
$form.Dispose()
