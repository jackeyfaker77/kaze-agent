param(
    [ValidateSet('Plan','Install','Start','Stop','Status','Uninstall')][string]$Action = 'Plan',
    [string]$Workspace = (Join-Path $PSScriptRoot '../workspace'),
    [ValidateRange(1024,65535)][int]$Port = 8765
)
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$Workspace = [IO.Path]::GetFullPath($Workspace).TrimEnd('\','/')
$python = Join-Path $repo '.venv/Scripts/python.exe'
$pythonw = Join-Path $repo '.venv/Scripts/pythonw.exe'
$entry = Join-Path $repo 'apps/backend/windows_host.py'
$private = Join-Path $Workspace 'private_runtime/windows-host'
$tokenFile = Join-Path $private 'runtime-token'
$manifestFile = Join-Path $private 'deployment.json'
$connectionFile = Join-Path $Workspace '.desktop/runtime-connection.json'
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$hash = [Security.Cryptography.SHA256]::Create()
try { $suffix = ([BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($Workspace.ToLowerInvariant())))).Replace('-','').Substring(0,12) } finally { $hash.Dispose() }
$taskName = "Kaze-Runtime-$suffix"
$url = "ws://127.0.0.1:$Port"
$guardStart = (Get-Date).AddSeconds(10).ToString('s')
function Escape-Xml([string]$value) { [Security.SecurityElement]::Escape($value) }
$arguments = (@($entry,'host','--workspace',$Workspace,'--config',(Join-Path $Workspace 'config.toml'),'--remote-port',"$Port",'--remote-token-file',$tokenFile) | ForEach-Object { '"' + $_ + '"' }) -join ' '
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
 <RegistrationInfo><Description>Kaze single-user localhost Runtime Host; managed by deploy/windows-runtime.ps1</Description></RegistrationInfo>
 <Triggers><LogonTrigger><Enabled>true</Enabled><Delay>PT10S</Delay><UserId>$(Escape-Xml $sid)</UserId></LogonTrigger><TimeTrigger><Repetition><Interval>PT1M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><StartBoundary>$guardStart</StartBoundary><Enabled>true</Enabled></TimeTrigger></Triggers>
 <Principals><Principal id="Author"><UserId>$(Escape-Xml $sid)</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
 <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><AllowHardTerminate>true</AllowHardTerminate><StartWhenAvailable>true</StartWhenAvailable><RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable><IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings><AllowStartOnDemand>true</AllowStartOnDemand><Enabled>false</Enabled><Hidden>true</Hidden><RunOnlyIfIdle>false</RunOnlyIfIdle><WakeToRun>false</WakeToRun><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Priority>7</Priority></Settings>
 <Actions Context="Author"><Exec><Command>$(Escape-Xml $pythonw)</Command><Arguments>$(Escape-Xml $arguments)</Arguments><WorkingDirectory>$(Escape-Xml $repo)</WorkingDirectory></Exec></Actions>
</Task>
"@
if ($Action -eq 'Plan') { @{taskName=$taskName; workspace=$Workspace; url=$url; tokenFile=$tokenFile; xml=$xml} | ConvertTo-Json -Depth 5; exit 0 }
foreach ($path in @($python,$pythonw,$entry,(Join-Path $Workspace 'config.toml'))) { if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Required file missing: $path" } }
function Get-HostState {
    $value = & $python (Join-Path $repo 'apps/backend/main.py') host-status --workspace $Workspace
    if ($LASTEXITCODE -ne 0) { throw 'Unable to read Host ownership' }
    $value | ConvertFrom-Json
}
function Write-JsonAtomic($path, $value) {
    $temp = "$path.$([Guid]::NewGuid().ToString('N')).tmp"
    try { [IO.File]::WriteAllText($temp, ($value | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false))); Move-Item -LiteralPath $temp -Destination $path -Force }
    finally { if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp } }
}
if ($Action -eq 'Install') {
    for ($cursor = New-Object IO.DirectoryInfo($Workspace); $null -ne $cursor; $cursor = $cursor.Parent) {
        if (Test-Path -LiteralPath (Join-Path $cursor.FullName '.kaze-restore-quarantine.json')) { throw 'An isolated restore cannot be installed as a live deployment.' }
    }
    if ((Get-HostState).running) { throw 'Stop the existing desktop backend/Host before installing.' }
    if ((Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) -or (Test-Path -LiteralPath $connectionFile) -or (Test-Path -LiteralPath $manifestFile)) { throw 'Existing installation or desktop connection detected; use Status/Start or explicitly Uninstall first.' }
    $probe = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback,$Port)
    try { $probe.Start() } finally { $probe.Stop() }
    New-Item -ItemType Directory -Force -Path $private | Out-Null
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true,$false)
    foreach ($identity in @($sid,'S-1-5-18')) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier($identity)), 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $private -AclObject $acl
    if (-not (Test-Path -LiteralPath $tokenFile)) {
        $bytes = New-Object byte[] 48
        $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        [IO.File]::WriteAllText($tokenFile,[Convert]::ToBase64String($bytes),(New-Object Text.UTF8Encoding($false)))
    }
    $token = [IO.File]::ReadAllText($tokenFile).Trim()
    if ($token -notmatch '^[\x21-\x7e]{32,512}$') { throw 'Existing token is invalid; no task registered.' }
    $token = $null
    New-Item -ItemType Directory -Force -Path (Split-Path $connectionFile) | Out-Null
    $registered = $false
    try {
        Register-ScheduledTask -TaskName $taskName -Xml $xml | Out-Null
        $registered = $true
        Write-JsonAtomic $manifestFile @{version=1;taskName=$taskName;workspace=$Workspace;repo=$repo;userSid=$sid;port=$Port;tokenFile=$tokenFile}
        Write-JsonAtomic $connectionFile @{version=1;url=$url;tokenFile=$tokenFile}
    } catch {
        if ($registered) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
        if (Test-Path -LiteralPath $manifestFile) { Remove-Item -LiteralPath $manifestFile }
        throw
    }
    @{installed=$true;taskName=$taskName;url=$url;autostart='current-user logon';restart='1 minute guard; IgnoreNew; normal-stop pause latch';started=$false} | ConvertTo-Json
    exit 0
}
if (-not (Test-Path -LiteralPath $manifestFile)) { throw 'No managed installation found.' }
$manifest = Get-Content -LiteralPath $manifestFile -Raw | ConvertFrom-Json
if ($manifest.taskName -ne $taskName -or $manifest.workspace -ne $Workspace -or $manifest.repo -ne $repo -or $manifest.userSid -ne $sid) { throw 'Deployment ownership does not match this workspace/user/repository.' }
$task = Get-ScheduledTask -TaskName $taskName
if ($Action -eq 'Status') {
    @{taskName=$taskName;state="$($task.State)";lastResult=(Get-ScheduledTaskInfo -TaskName $taskName).LastTaskResult;host=(Get-HostState)} | ConvertTo-Json -Depth 8
} elseif ($Action -eq 'Start') {
    # Restore desired enabled state even if a previous Stop timed out while the
    # process was alive. Do not launch a second instance of a running task.
    $pauseFile=Join-Path $private 'paused.json'
    if (Test-Path -LiteralPath $pauseFile) { Remove-Item -LiteralPath $pauseFile }
    if (-not $task.Settings.Enabled) { Enable-ScheduledTask -TaskName $taskName | Out-Null }
    if ($task.State -ne 'Running') {
        Start-ScheduledTask -TaskName $taskName
    }
    @{requested=$true;taskName=$taskName} | ConvertTo-Json
} elseif ($Action -eq 'Stop' -or $Action -eq 'Uninstall') {
    # Disable first, so a failure while stopping cannot schedule another start.
    Disable-ScheduledTask -TaskName $taskName | Out-Null
    Write-JsonAtomic (Join-Path $private 'paused.json') @{reason='Explicit deployment Stop; use Start to resume'}
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    $stopOwner = $null
    do {
        $state=Get-HostState
        $runningTask=(Get-ScheduledTask -TaskName $taskName).State -in @('Running','Queued')
        if ($state.running -and $state.mode) {
            if ($state.mode -ne 'host') { throw 'A desktop bridge owns this workspace; it must be closed by its client.' }
            if ($state.owner_id -and $state.owner_id -ne $stopOwner) {
                & $python (Join-Path $repo 'apps/backend/main.py') host-stop --workspace $Workspace
                if ($LASTEXITCODE -ne 0) { throw 'Host stop request failed' }
                $stopOwner=$state.owner_id
            }
        }
        if (-not $state.running -and -not $runningTask) { break }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    if ($state.running -or $runningTask) { throw 'Host/task still active; no force kill or data deletion performed.' }
    if ($Action -eq 'Uninstall') {
        if (Test-Path -LiteralPath $connectionFile) {
            $connection=Get-Content -LiteralPath $connectionFile -Raw | ConvertFrom-Json
            if ($connection.version -ne 1 -or $connection.tokenFile -ne $manifest.tokenFile -or $connection.url -ne "ws://127.0.0.1:$($manifest.port)") { throw 'Desktop connection changed; refusing to remove it.' }
            Remove-Item -LiteralPath $connectionFile
        }
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Remove-Item -LiteralPath $manifestFile
    }
    @{stopped=$true;uninstalled=($Action -eq 'Uninstall');dataPreserved=$true} | ConvertTo-Json
}
