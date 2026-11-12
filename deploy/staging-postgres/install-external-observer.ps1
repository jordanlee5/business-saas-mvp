param(
    [Parameter(Mandatory=$true)][string]$PrivateRoot,
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$OffsiteConfig,
    [string]$TaskName = 'SaaS-Staging-External-Observer'
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$PrivateRoot = [IO.Path]::GetFullPath($PrivateRoot)
if ($PrivateRoot.StartsWith($repo + '\', [StringComparison]::OrdinalIgnoreCase) -or $PrivateRoot -eq $repo) { throw 'Use a private directory outside source repository' }
if (Test-Path -LiteralPath $PrivateRoot) { throw 'Existing directory refused' }
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { throw 'Existing task refused' }
foreach ($item in @($Python,$OffsiteConfig)) { if (-not (Test-Path -LiteralPath $item -PathType Leaf)) { throw 'Required file missing' } }
$parent = Split-Path $PrivateRoot
while ($parent) {
    if ((Test-Path -LiteralPath $parent) -and ((Get-Item -LiteralPath $parent).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Linked parent refused' }
    $parent = Split-Path $parent
}
$existing = Get-Content -LiteralPath $OffsiteConfig -Raw | ConvertFrom-Json
if ($existing.project -ne 'saas-pg-test-cvm') { throw 'Dedicated project required' }
$null = New-Item -ItemType Directory -Path $PrivateRoot
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$sid = $identity.User.Value
& icacls $PrivateRoot /inheritance:r /grant:r "*${sid}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Private directory ACL failed' }
$runner = Join-Path $PrivateRoot 'runner'
$modules = Join-Path $runner 'tools'
$null = New-Item -ItemType Directory -Path $modules
foreach ($name in @('staging_external_observer.py','staging_backup_job.py','staging_backup_watch.py','staging_offsite_backup.py','staging_pg_backup.py','staging_monitor.py','staging_backup_crypto.py')) {
    Copy-Item -LiteralPath (Join-Path $repo ('tools/' + $name)) -Destination $modules
}
$configFile = Join-Path $PrivateRoot 'config.json'
$config = @{ project=$existing.project; ssh=$existing.ssh; key=$existing.key; known_hosts=$existing.known_hosts; host=$existing.host; state_root=(Join-Path $PrivateRoot 'state') }
[IO.File]::WriteAllText($configFile, ($config | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
$action = New-ScheduledTaskAction -Execute $Python -Argument ('-m tools.staging_external_observer --config "' + $configFile + '"') -WorkingDirectory $runner
$periodic = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes 5)
$logon = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $sid -LogonType Interactive -RunLevel Limited
$null = Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($periodic,$logon) -Settings $settings -Principal $principal -Description 'Local-only read-only external observer. No outgoing notifications, no backup or service changes. Requires signed-in online PC.'
Write-Output 'Local-only observer installed. No notification credentials copied. Inspect state/latest.json after first run.'
