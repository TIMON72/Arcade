# Sync local Arcade tree to a Batocera cabinet, then run on-device deploy.
# Usage:
#   .\deploy\deploy.ps1 zero
#   .\deploy\deploy.ps1 zero -Full -Restart
#   .\deploy\deploy.ps1 zero -NoLogs

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Target,
    [string]$SshUser,
    [string]$Auth,
    [string]$RemotePath,
    [string]$RepoRoot,
    [switch]$Full,
    [switch]$NoDeploy,
    [switch]$Restart,
    [switch]$NoLogs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Read-MachinesConfig {
    $path = Join-Path $PSScriptRoot 'machines.json'
    if (-not (Test-Path -LiteralPath $path)) {
        return $null
    }
    return Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Test-DeployIpv4Name {
    param([string]$Name)
    $parts = $Name.Split('.')
    if ($parts.Count -ne 4) {
        return $false
    }
    foreach ($part in $parts) {
        if ($part.Length -lt 1) {
            return $false
        }
        if ($part.Length -gt 3) {
            return $false
        }
        foreach ($ch in $part.ToCharArray()) {
            if ($ch -lt '0') {
                return $false
            }
            if ($ch -gt '9') {
                return $false
            }
        }
        $n = [int]$part
        if ($n -lt 0) {
            return $false
        }
        if ($n -gt 255) {
            return $false
        }
    }
    return $true
}

function Resolve-TargetHost {
    param(
        [string]$Name,
        $Config
    )
    if (Test-DeployIpv4Name -Name $Name) {
        return $Name
    }
    if ($null -eq $Config) {
        throw "Unknown target $Name. Use an IP or deploy/machines.json."
    }
    if ($null -eq $Config.machines) {
        throw "Unknown target $Name. Use an IP or deploy/machines.json."
    }
    $prop = $Config.machines.PSObject.Properties[$Name]
    if ($null -eq $prop) {
        $known = @($Config.machines.PSObject.Properties.Name) -join ', '
        throw "Unknown machine $Name. Known: $known"
    }
    return [string]$prop.Value
}

function New-AskPassScript {
    param([string]$Text)
    $askCmd = Join-Path $env:TEMP ('arcade-askpass-' + $PID + '.cmd')
    $askPs1 = Join-Path $env:TEMP ('arcade-askpass-' + $PID + '.ps1')
    # Без CR/LF: echo/set /p под OpenSSH ASKPASS на Windows часто ломают пароль.
    $ps = '[Console]::Out.Write(' + ("'" + $Text.Replace("'", "''") + "'") + ')'
    Set-Content -LiteralPath $askPs1 -Value $ps -Encoding ASCII
    $line1 = '@echo off'
    $line2 = 'powershell -NoProfile -ExecutionPolicy Bypass -File "' + $askPs1 + '"'
    Set-Content -LiteralPath $askCmd -Value @($line1, $line2) -Encoding ASCII
    return $askCmd
}

function Invoke-SshNative {
    param(
        [string]$Binary,
        $Arguments,
        [string]$AskCmd,
        [string]$FailMessage
    )
    $prevAsk = $env:SSH_ASKPASS
    $prevReq = $env:SSH_ASKPASS_REQUIRE
    $prevDisp = $env:DISPLAY
    $prevEa = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($AskCmd) {
            $env:SSH_ASKPASS = $AskCmd
            $env:SSH_ASKPASS_REQUIRE = 'force'
            $env:DISPLAY = '1'
        }
        & $Binary @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw ($FailMessage + ' (exit ' + $LASTEXITCODE + ')')
        }
    }
    finally {
        $ErrorActionPreference = $prevEa
        if ($null -eq $prevAsk) {
            Remove-Item Env:\SSH_ASKPASS -ErrorAction SilentlyContinue
        }
        else {
            $env:SSH_ASKPASS = $prevAsk
        }
        if ($null -eq $prevReq) {
            Remove-Item Env:\SSH_ASKPASS_REQUIRE -ErrorAction SilentlyContinue
        }
        else {
            $env:SSH_ASKPASS_REQUIRE = $prevReq
        }
        if ($null -eq $prevDisp) {
            Remove-Item Env:\DISPLAY -ErrorAction SilentlyContinue
        }
        else {
            $env:DISPLAY = $prevDisp
        }
    }
}

function Invoke-Remote {
    param(
        [string]$SshTarget,
        $SshBaseArgs,
        [string]$RemoteCommand,
        [string]$AskCmd
    )
    $argsList = @($SshBaseArgs) + @($SshTarget, $RemoteCommand)
    Invoke-SshNative -Binary ssh -Arguments $argsList -AskCmd $AskCmd -FailMessage ('ssh failed: ' + $RemoteCommand)
}

function Send-File {
    param(
        [string]$SshTarget,
        $SshBaseArgs,
        [string]$LocalPath,
        [string]$RemoteFile,
        [string]$AskCmd
    )
    $remoteSpec = $SshTarget + ':' + $RemoteFile
    $argsList = @($SshBaseArgs) + @($LocalPath, $remoteSpec)
    Invoke-SshNative -Binary scp -Arguments $argsList -AskCmd $AskCmd -FailMessage 'scp failed'
}

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}

$config = Read-MachinesConfig
$cabinetHost = Resolve-TargetHost -Name $Target -Config $config

if (-not $SshUser) {
    if ($config -and $config.user) {
        $SshUser = [string]$config.user
    }
    else {
        $SshUser = 'root'
    }
}
if (-not $RemotePath) {
    if ($config -and $config.remotePath) {
        $RemotePath = [string]$config.remotePath
    }
    else {
        $RemotePath = '/userdata/system/Arcade'
    }
}
if (-not $PSBoundParameters.ContainsKey('Auth')) {
    if ($env:ARCADE_SSH_PASSWORD) {
        $Auth = $env:ARCADE_SSH_PASSWORD
    }
    elseif ($config -and $null -ne $config.password) {
        $Auth = [string]$config.password
    }
    else {
        $Auth = 'linux'
    }
}

if ([string]::IsNullOrEmpty($Auth)) {
    throw 'SSH password required. Use -Auth, machines.json password, or ARCADE_SSH_PASSWORD.'
}

$sshTarget = $SshUser + '@' + $cabinetHost
# После смены NVMe Dropbear выдаёт новые host keys — сбрасываем known_hosts.
Write-Host ('... clearing SSH host keys for ' + $cabinetHost)
$prevEa = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    & ssh-keygen -R $cabinetHost 1>$null 2>$null
    & ssh-keygen -R ('[' + $cabinetHost + ']:22') 1>$null 2>$null
    if ($Target -ne $cabinetHost) {
        & ssh-keygen -R $Target 1>$null 2>$null
    }
}
finally {
    $ErrorActionPreference = $prevEa
}

$sshBaseArgs = @(
    '-o', ('HostName=' + $cabinetHost),
    '-o', 'StrictHostKeyChecking=no',
    '-o', 'UserKnownHostsFile=NUL',
    '-o', 'GlobalKnownHostsFile=NUL',
    '-o', 'UpdateHostKeys=no',
    '-o', 'PreferredAuthentications=password',
    '-o', 'PubkeyAuthentication=no',
    '-o', 'Ciphers=aes128-ctr,aes256-ctr',
    '-o', 'MACs=hmac-sha2-256,hmac-sha1',
    '-o', 'ConnectTimeout=15',
    '-o', 'LogLevel=ERROR'
)

Write-Host ('-> target ' + $sshTarget)
Write-Host ('-> remote ' + $RemotePath)
Write-Host ('-> repo   ' + $RepoRoot)
Write-Host '... password auth'

$askCmd = New-AskPassScript -Text $Auth

$tarPath = Join-Path $env:TEMP ('arcade-sync-' + $PID + '.tar')
$remoteTar = '/tmp/arcade-sync-' + $PID + '.tar'

$excludeArgs = @(
    '--exclude=.git',
    '--exclude=.venv',
    '--exclude=venv',
    '--exclude=__pycache__',
    '--exclude=logs.log',
    '--exclude=.arcade-deployed',
    '--exclude=.cursor',
    '--exclude=*.pyc',
    '--exclude=deploy/machines.json'
)
if (-not $Full) {
    $excludeArgs += @('--exclude=wheels', '--exclude=vendor')
}

$syncedOk = $false
try {
    Write-Host ('... packing (Full=' + $Full + ')...')
    # ustar: safer across Windows OpenSSH tar ↔ Batocera GNU/BusyBox tar
    & tar --format=ustar -cf $tarPath @excludeArgs -C $RepoRoot .
    if ($LASTEXITCODE -ne 0) {
        throw ('tar create failed (exit ' + $LASTEXITCODE + ')')
    }

    $sizeMb = [math]::Round((Get-Item -LiteralPath $tarPath).Length / 1MB, 2)
    Write-Host ('... uploading ' + $sizeMb + ' MB...')
    Send-File -SshTarget $sshTarget -SshBaseArgs $sshBaseArgs -LocalPath $tarPath -RemoteFile $remoteTar -AskCmd $askCmd

    # BusyBox tar often skips existing files — wipe sync targets before extract
    # so empty stubs from a bad first sync cannot stick around.
    $remoteCmd = (
        'set -e; mkdir -p ' + "'" + $RemotePath + "'" +
        '; rm -rf ' + "'" + $RemotePath + "/services' '" + $RemotePath + "/scripts' '" +
        $RemotePath + "/configs'" +
        '; tar -xf ' + "'" + $remoteTar + "'" + ' -C ' + "'" + $RemotePath + "'" +
        '; rm -f ' + "'" + $remoteTar + "'"
    )
    if (-not $NoDeploy) {
        $remoteCmd = $remoteCmd + '; python3 ' + "'" + $RemotePath + '/scripts/main/main.py' + "'" + ' deploy'
    }
    if ($Restart) {
        $remoteCmd = $remoteCmd + '; batocera-services restart main timer server tvon || true'
    }

    $extractMsg = 'extracting'
    if (-not $NoDeploy) {
        $extractMsg = $extractMsg + ' + deploy'
    }
    if ($Restart) {
        $extractMsg = $extractMsg + ' + restart'
    }
    Write-Host ('... ' + $extractMsg)
    Invoke-Remote -SshTarget $sshTarget -SshBaseArgs $sshBaseArgs -RemoteCommand $remoteCmd -AskCmd $askCmd

    Write-Host ('OK synced to ' + $cabinetHost)
    if (-not $NoDeploy) {
        Write-Host ('  Next: Cursor Remote-SSH -> ' + $sshTarget + ' -> open ' + $RemotePath + ' -> F5 Arcade')
    }
    $syncedOk = $true
}
finally {
    Remove-Item -LiteralPath $tarPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $askCmd -Force -ErrorAction SilentlyContinue
}

if ($syncedOk -and -not $NoLogs) {
    Write-Host ''
    Write-Host '... attaching logs (Ctrl+C to stop)'
    $logsScript = Join-Path $PSScriptRoot 'logs.ps1'
    & $logsScript -Target $Target -SshUser $SshUser -Auth $Auth
}
