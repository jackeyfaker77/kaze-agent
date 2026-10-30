param([string]$Scenario, [string]$TestDirectory)
$ErrorActionPreference='Stop'
$source=Get-Content (Join-Path $PSScriptRoot '../../deploy/windows-runtime.ps1') -Raw
$startAnchor='} elseif ($Action -eq ''Start'') {'
$stopAnchor='} elseif ($Action -eq ''Stop'' -or $Action -eq ''Uninstall'') {'
$startAt=$source.IndexOf($startAnchor)
$stopAt=$source.IndexOf($stopAnchor)
if ($startAt -lt 0 -or $stopAt -lt 0) { throw 'Management branches not found' }

# Execute the shipped management branch with deterministic scheduler/Host states.
# No real task or process is changed; the timeout alone is shortened for tests.
$global:checks=0; $global:requests=@(); $global:enabled=0; $global:started=0; $global:disabled=$false
$taskName='isolated-fixture'; $repo=$PSScriptRoot; $Workspace=$TestDirectory; $private=$TestDirectory
$python='Invoke-FakePython'
$pauseFile=Join-Path $private 'paused.json'
function Write-JsonAtomic($path,$value) { $value | ConvertTo-Json | Set-Content -LiteralPath $path }
function Disable-ScheduledTask { param($TaskName) $global:disabled=$true }
function Enable-ScheduledTask { param($TaskName) $global:enabled++ }
function Start-ScheduledTask { param($TaskName) $global:started++ }
function Start-Sleep { param($Milliseconds) }
function Get-HostState {
    $global:checks++
    $index=[Math]::Min($global:checks-1,$states.Count-1)
    return $states[$index].host
}
function Get-ScheduledTask {
    param($TaskName)
    $index=[Math]::Min($global:checks-1,$states.Count-1)
    return @{State=$states[$index].task}
}
function Invoke-FakePython {
    param($entry,$command)
    if ($command -ne 'host-stop') { throw 'Unexpected process operation' }
    if (-not $global:disabled -or -not (Test-Path -LiteralPath $pauseFile)) { throw 'Stop intent must precede process control' }
    $global:requests+= $state.owner_id
    $global:LASTEXITCODE=0
}
$off=@{running=$false}
$a=@{running=$true;mode='host';owner_id='a'}
$b=@{running=$true;mode='host';owner_id='b'}
if ($Scenario.EndsWith('_start')) {
    $Action='Start'
    $task=@{State='Running';Settings=@{Enabled=($Scenario -eq 'enabled_running_start')}}
    if (-not $task.Settings.Enabled) { Write-JsonAtomic $pauseFile @{reason='Stop timed out'} }
    $body=$source.Substring($startAt+$startAnchor.Length,$stopAt-$startAt-$startAnchor.Length)
    & ([scriptblock]::Create($body)) | Out-Null
    $expected=if ($Scenario -eq 'disabled_running_start') { 1 } else { 0 }
    if ($global:enabled -ne $expected -or $global:started -ne 0 -or (Test-Path -LiteralPath $pauseFile)) { throw 'Running Start did not reconcile enabled state without a duplicate launch' }
} else {
    $Action='Stop'
    switch ($Scenario) {
        'startup' { $states=@(@{host=$off;task='Running'},@{host=$a;task='Running'},@{host=$a;task='Running'},@{host=$off;task='Ready'}); $expected=@('a') }
        'owner_change' { $states=@(@{host=$a;task='Running'},@{host=$a;task='Running'},@{host=$b;task='Running'},@{host=$off;task='Ready'}); $expected=@('a','b') }
        'queued' { $states=@(@{host=$off;task='Queued'},@{host=$off;task='Ready'}); $expected=@() }
        'timeout' { $states=@(@{host=$a;task='Running'}); $expected=@('a') }
        default { throw 'Unknown scenario' }
    }
    $body=$source.Substring($stopAt+$stopAnchor.Length,$source.LastIndexOf('}')-$stopAt-$stopAnchor.Length).Replace('AddSeconds(45)','AddSeconds(0.2)')
    $failure=$null
    try { & ([scriptblock]::Create($body)) | Out-Null } catch { $failure=$_.Exception.Message }
    if ($Scenario -eq 'timeout') {
        if ($failure -notlike 'Host/task still active*') { throw "Expected safe timeout, got: $failure" }
    } elseif ($failure) { throw $failure }
    if (-not $global:disabled -or -not (Test-Path -LiteralPath $pauseFile)) { throw 'Stop intent lost' }
    if (($global:requests -join ',') -ne ($expected -join ',')) { throw 'Missing/duplicate/wrong-owner stop request' }
    if ($Scenario -ne 'timeout' -and $global:checks -ne $states.Count) { throw 'Stopped waiting before pending task/Host exited' }
}
@{scenario=$Scenario;passed=$true}|ConvertTo-Json -Compress
