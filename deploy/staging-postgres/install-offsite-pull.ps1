param(
    [Parameter(Mandatory=$true)][string]$PrivateRoot,
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$KeyFile,
    [Parameter(Mandatory=$true)][string]$KnownHosts,
    [Parameter(Mandatory=$true)][string]$SshHost,
    [string]$Project = 'saas-pg-test-cvm',
    [string]$RemoteCode = '/srv/saas-staging/offsite-ops-20261111',
    [string]$RemoteRoot = '/srv/saas-backups/scheduled/saas-pg-test-cvm',
    [string]$TaskName = 'SaaS-Staging-Offsite-Backup'
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$PrivateRoot = [IO.Path]::GetFullPath($PrivateRoot)
if ($PrivateRoot.StartsWith($repo + '\', [StringComparison]::OrdinalIgnoreCase) -or $PrivateRoot -eq $repo) { throw 'Private directory must be outside source repository' }
if (Test-Path -LiteralPath $PrivateRoot) { throw 'Choose a new dedicated private directory; existing installation is not overwritten' }
foreach ($item in @($Python, $KeyFile, $KnownHosts)) { if (-not (Test-Path -LiteralPath $item -PathType Leaf)) { throw 'Required local file missing' } }
$Python = (Resolve-Path -LiteralPath $Python).Path
$PythonWindowless = Join-Path (Split-Path $Python) 'pythonw.exe'
$sshExecutable = Join-Path $env:WINDIR 'System32/OpenSSH/ssh.exe'
foreach ($required in @($PythonWindowless, $sshExecutable)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { throw 'Python with pythonw.exe and Windows OpenSSH client are required' }
}
$parent = Split-Path $PrivateRoot
while ($parent) {
    if ((Test-Path -LiteralPath $parent) -and ((Get-Item -LiteralPath $parent).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Linked parent refused' }
    $parent = Split-Path $parent
}
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { throw 'Task already exists; inspect before replacing' }
$null = New-Item -ItemType Directory -Path $PrivateRoot
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$sid = $identity.User.Value
& icacls $PrivateRoot /inheritance:r /grant:r "*${sid}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Private directory ACL setup failed' }
$runner = Join-Path $PrivateRoot 'runner'
$modules = Join-Path $runner 'tools'
$null = New-Item -ItemType Directory -Path $modules
foreach ($name in @('windows_background.py','staging_offsite_backup.py','staging_backup_job.py','staging_pg_backup.py','staging_backup_crypto.py','staging_backup_watch.py','staging_monitor.py')) {
    Copy-Item -LiteralPath (Join-Path $repo ('tools/' + $name)) -Destination $modules
}
$hostsCopy = Join-Path $PrivateRoot 'known_hosts'
Copy-Item -LiteralPath $KnownHosts -Destination $hostsCopy
$configFile = Join-Path $PrivateRoot 'config.json'
$config = @{ project=$Project; local_root=(Join-Path $PrivateRoot 'copies'); ssh=(Join-Path $env:WINDIR 'System32/OpenSSH/ssh.exe'); key=[IO.Path]::GetFullPath($KeyFile); known_hosts=$hostsCopy; host=$SshHost; remote_code=$RemoteCode; remote_root=$RemoteRoot; keep=14 }
[IO.File]::WriteAllText($configFile, ($config | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
$action = New-ScheduledTaskAction -Execute $PythonWindowless -Argument ('-m tools.staging_offsite_backup pull --config "' + $configFile + '"') -WorkingDirectory $runner
$hourly = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Hours 1)
$logon = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $sid -LogonType Interactive -RunLevel Limited
$null = Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($hourly,$logon) -Settings $settings -Principal $principal -Description 'Pull verified staging backups while this Windows user is signed in; keep 14 points. No stored password; no server restart.'
Write-Output 'Private runner and hourly/logon task installed. Start task and verify last-run.json before enabling cloud offsite alerts.'
