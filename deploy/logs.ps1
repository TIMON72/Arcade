# Tail Arcade shared logs.log on a Batocera cabinet.
# Usage: .\deploy\logs.ps1 zero
#        .\deploy\logs.ps1 zero -SinceMinutes 10

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Target,
    [string]$SshUser,
    [string]$Auth,
    [switch]$AllServices,
    [ValidateSet('main', 'timer', 'server', 'tvon')]
    [string]$Only,
    [int]$RetrySeconds = 3,
    [int]$SinceMinutes = 5
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-ArcadeMachinesConfig {
    $path = Join-Path $PSScriptRoot 'machines.json'
    if (-not (Test-Path -LiteralPath $path)) {
        return $null
    }
    return Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Test-ArcadeIpv4Name {
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

function Resolve-ArcadeHost {
    param(
        [string]$Name,
        $Config
    )
    if (Test-ArcadeIpv4Name -Name $Name) {
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

function New-ArcadeAskPass {
    param([string]$Text)
    $path = Join-Path $env:TEMP ('arcade-askpass-' + $PID + '.cmd')
    $askPs1 = Join-Path $env:TEMP ('arcade-askpass-' + $PID + '.ps1')
    # Без CR/LF: echo/set /p под OpenSSH ASKPASS на Windows часто ломают пароль.
    $ps = '[Console]::Out.Write(' + ("'" + $Text.Replace("'", "''") + "'") + ')'
    Set-Content -LiteralPath $askPs1 -Value $ps -Encoding ASCII
    $line1 = '@echo off'
    $line2 = 'powershell -NoProfile -ExecutionPolicy Bypass -File "' + $askPs1 + '"'
    Set-Content -LiteralPath $path -Value @($line1, $line2) -Encoding ASCII
    return $path
}

function Invoke-ArcadeLogTail {
    param(
        [string]$SshTarget,
        $BaseArgs,
        [string]$RemoteCommand
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & ssh @BaseArgs $SshTarget $RemoteCommand
        return $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $prev
    }
}

$config = Get-ArcadeMachinesConfig
$cabinetHost = Resolve-ArcadeHost -Name $Target -Config $config

if (-not $SshUser) {
    if ($config -and $config.user) {
        $SshUser = [string]$config.user
    }
    else {
        $SshUser = 'root'
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

$mode = 'app'
if ($AllServices) {
    $mode = 'all'
}
elseif ($Only) {
    $mode = $Only
}

switch ($mode) {
    'app' {
        $logPaths = @('/userdata/system/scripts/logs.log')
    }
    'main' {
        $logPaths = @('/userdata/system/logs/main-service.log')
    }
    'timer' {
        $logPaths = @('/userdata/system/logs/timer-service.log')
    }
    'server' {
        $logPaths = @('/userdata/system/logs/server-service.log')
    }
    'tvon' {
        $logPaths = @('/userdata/system/logs/tvon-service.log')
    }
    default {
        $logPaths = @(
            '/userdata/system/scripts/logs.log',
            '/userdata/system/logs/main-service.log',
            '/userdata/system/logs/timer-service.log',
            '/userdata/system/logs/server-service.log',
            '/userdata/system/logs/tvon-service.log'
        )
    }
}

if ([string]::IsNullOrEmpty($Auth)) {
    throw 'SSH password required. Use -Auth, machines.json password, or ARCADE_SSH_PASSWORD.'
}

$sshTarget = $SshUser + '@' + $cabinetHost
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
    '-o', 'ServerAliveInterval=5',
    '-o', 'ServerAliveCountMax=3',
    '-o', 'LogLevel=ERROR'
)

$askCmdPath = New-ArcadeAskPass -Text $Auth
$env:SSH_ASKPASS = $askCmdPath
$env:SSH_ASKPASS_REQUIRE = 'force'
$env:DISPLAY = '1'

$pathList = $logPaths -join ' '
# Inline filter (was deploy/log-since.py): print lines from last N minutes, then follow.
$sincePy = @'
from datetime import datetime, timedelta
import sys
cut = datetime.now() - timedelta(minutes=max(0, int(sys.argv[1])))
for path in sys.argv[2:]:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    stamp = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                if stamp >= cut:
                    sys.stdout.write(line)
    except OSError as error:
        sys.stderr.write("warn: %s: %s\n" % (path, error))
'@
$sinceB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($sincePy))
# Pass filter via base64|sh so remote bash does not strip python -c quotes.
$remoteCmd = (
    'echo ' + $sinceB64 +
    ' | base64 -d > /tmp/arcade-log-since.py && python3 /tmp/arcade-log-since.py ' +
    $SinceMinutes + ' ' + $pathList +
    '; echo LIVE; tail -n 0 -F ' + $pathList
)

Write-Host ('-> logs on ' + $sshTarget + ' mode=' + $mode + ' since=' + $SinceMinutes + 'm then follow')
Write-Host ('  remote: ' + $pathList)
Write-Host '  auto-reconnect with backoff; Ctrl+C to stop'

$attempt = 0
$failStreak = 0
$delay = [Math]::Max(1, $RetrySeconds)
$maxDelay = 60
$maxFailStreak = 8
try {
    while ($true) {
        $attempt += 1
        if ($attempt -gt 1) {
            $now = Get-Date -Format 'HH:mm:ss'
            Write-Host ''
            Write-Host ('[' + $now + '] reconnecting to ' + $sshTarget + ' attempt=' + $attempt + ' delay=' + $delay + 's')
        }

        $exitCode = Invoke-ArcadeLogTail -SshTarget $sshTarget -BaseArgs $sshBaseArgs -RemoteCommand $remoteCmd
        $now = Get-Date -Format 'HH:mm:ss'

        # 255 / быстрый обрыв = Dropbear/KEX; частые попытки роняют автомат (чёрный экран).
        if ($exitCode -eq 0) {
            $failStreak = 0
            $delay = [Math]::Max(1, $RetrySeconds)
        }
        else {
            $failStreak += 1
            $delay = [Math]::Min($maxDelay, [Math]::Max($RetrySeconds, $delay * 2))
        }

        if ($failStreak -ge $maxFailStreak) {
            Write-Host ('[' + $now + '] ssh failed ' + $failStreak + ' times in a row (last exit=' + $exitCode + '). Stopping to avoid Dropbear/hang storm.')
            Write-Host '  Reboot cabinet if needed, then run logs.ps1 again.'
            break
        }

        Write-Host ('[' + $now + '] ssh ended exit=' + $exitCode + '; retry in ' + $delay + 's (failStreak=' + $failStreak + ')')
        Start-Sleep -Seconds $delay
    }
}
finally {
    Remove-Item Env:\SSH_ASKPASS -ErrorAction SilentlyContinue
    Remove-Item Env:\SSH_ASKPASS_REQUIRE -ErrorAction SilentlyContinue
    Remove-Item Env:\DISPLAY -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $askCmdPath -Force -ErrorAction SilentlyContinue
    $askPs1Path = [System.IO.Path]::ChangeExtension($askCmdPath, '.ps1')
    if ($askPs1Path) {
        Remove-Item -LiteralPath $askPs1Path -Force -ErrorAction SilentlyContinue
    }
}
